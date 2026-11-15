"""Helpers shared by the venv build and the task runner."""

from __future__ import annotations

import base64
import ctypes
import errno
import itertools
import logging
import os
import random
import signal
import stat
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from lakana_worker.processes import (
    CappedSink,
    OutputRedactor,
    UnsafeDirectory,
    is_private_directory,
    prepare_private_directory,
    remove_private_tree,
    restore_directory_access,
    signal_group,
    tree_size,
)


def _collect(secrets: list[str], chunks: list[bytes], max_line: int = 65536) -> bytes:
    """Feed chunks through a redactor.

    Parameters
    ----------
    secrets
        Values to hide.
    chunks
        The reads, in order.
    max_line
        Longest line buffered.

    Returns
    -------
    bytes
        Everything the sink received.
    """
    out: list[bytes] = []
    redactor = OutputRedactor(secrets, out.append, max_line=max_line)
    for chunk in chunks:
        redactor.feed(chunk)
    redactor.close()
    redactor.close()
    return b"".join(out)


def test_secrets_and_userinfo_are_redacted() -> None:
    out = _collect(
        ["hunter2", "svc:hunter2", ""],
        [b"proxy http://svc:hunter2@proxy.corp:3128 said hunter2\n", b"plain line\n"],
    )
    assert out == b"proxy http://***@proxy.corp:3128 said ***\nplain line\n"


def test_a_secret_split_across_two_reads_is_still_redacted() -> None:
    assert _collect(["lakana_task_k1_s3cr3t"], [b"token lakana_task_k", b"1_s3cr3t end\n"]) == (
        b"token *** end\n"
    )


def test_an_unterminated_tail_is_flushed_on_close() -> None:
    assert _collect(["s3cr3t"], [b"no newline s3cr3t"]) == b"no newline ***"
    assert _collect([], []) == b""


def test_a_very_long_line_is_flushed_before_its_newline() -> None:
    out = _collect(["s3cr3t"], [b"a" * 10, b"s3cr3t", b"b" * 10, b"\n"], max_line=12)
    assert b"s3cr3t" not in out
    assert out.count(b"a") == 10 and out.count(b"b") == 10 and out.endswith(b"\n")


def test_tree_size_sums_regular_files_without_following_links(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "b").write_bytes(b"123")
    (tmp_path / "link").symlink_to("/etc")
    assert tree_size(tmp_path) >= 8
    assert tree_size(tmp_path) < 4096
    assert tree_size(tmp_path / "missing") == 0


def test_signal_group_kills_the_whole_group_and_ignores_a_reaped_process() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    assert signal_group(process, signal.SIGKILL) is True
    assert process.wait(timeout=10) == -signal.SIGKILL
    assert signal_group(process, signal.SIGKILL) is False


def test_signal_group_says_when_the_group_is_already_gone() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=10)
    process.returncode = None  # as if not yet reaped: the group is gone all the same
    assert signal_group(process, signal.SIGKILL) is False
    process.returncode = 0


def test_signal_group_treats_a_group_of_exited_processes_as_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS, signalling a group whose members have all exited but are not yet reaped fails."""
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    process.wait(timeout=10)
    process.returncode = None

    def refuse(pgid: int, signum: int) -> None:
        """Fail as macOS's killpg does for a group of zombies.

        Parameters
        ----------
        pgid
            The process group.
        signum
            The signal.

        Raises
        ------
        PermissionError
            Always.
        """
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", refuse)
    assert signal_group(process, signal.SIGKILL) is False
    process.returncode = 0


_SECRET = "hunter2secret"


@pytest.mark.parametrize("offset", range(1, len(_SECRET)))
def test_a_secret_straddling_the_forced_flush_is_redacted(offset: int) -> None:
    out = _collect(
        [_SECRET],
        [b"a" * 20 + _SECRET.encode()[:offset], _SECRET.encode()[offset:] + b" tail\n"],
        max_line=12,
    )
    assert _SECRET.encode() not in out and _SECRET.encode()[:offset] not in out.replace(
        b"a" * 20, b""
    )
    assert out == b"a" * 20 + b"*** tail\n"


_URL = b"http://svc:pw12@pypi.corp/x"


@pytest.mark.parametrize("offset", range(1, len(_URL)))
def test_userinfo_straddling_the_forced_flush_is_redacted(offset: int) -> None:
    out = _collect([], [b"a" * 60 + b" " + _URL[:offset], _URL[offset:] + b"\n"], max_line=40)
    assert b"svc" not in out and b"pw12" not in out
    assert out == b"a" * 60 + b" http://***@pypi.corp/x\n"


def test_a_secret_with_a_line_break_is_redacted_across_two_reads() -> None:
    secret = "abc\ndefgh"
    assert _collect([secret], [b"x abc\n", b"defgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc\nde", b"fgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc", b"\ndefgh y\n"]) == b"x *** y\n"
    assert _collect([secret], [b"x abc\nzzz\n", b"defgh\n"]) == b"x abc\nzzz\ndefgh\n"


def test_a_secret_with_a_line_break_holds_back_at_most_one_byte_fewer_than_its_length() -> None:
    secret = "abc\ndefgh"
    out: list[bytes] = []
    redactor = OutputRedactor([secret], out.append)
    for _ in range(200):
        redactor.feed(b"a line of ordinary output\n")
        assert len(redactor._buffer) <= len(secret) - 1
    redactor.close()
    assert b"".join(out) == b"a line of ordinary output\n" * 200


def test_an_endless_scheme_prefix_does_not_hold_the_buffer_forever() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor([], out.append, max_line=12)
    redactor.feed(b"http://" + b"x" * 100)
    assert len(b"".join(out)) > 50
    redactor.close()
    assert b"".join(out) == b"http://" + b"x" * 100


def test_output_that_may_be_the_start_of_a_secret_waits_and_other_output_does_not() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor(["x" * 40], out.append, max_line=12)
    redactor.feed(b"x" * 13)
    assert out == []
    redactor.close()
    assert b"".join(out) == b"x" * 13
    redactor.feed(b"y" * 13)
    assert b"".join(out) == b"x" * 13 + b"y" * 13


def test_the_userinfo_pattern_has_no_unbounded_run_before_the_scheme_separator() -> None:
    from lakana_worker.processes import _USERINFO

    assert b"+://" not in _USERINFO.pattern and b"*://" not in _USERINFO.pattern
    assert b"\\w+" not in _USERINFO.pattern


def test_a_huge_run_of_word_characters_is_linear_and_unchanged() -> None:
    import time

    data = b"A" * (256 * 1024)
    started = time.monotonic()
    out = _collect([], [data, b"\n"])
    assert out == data + b"\n"
    assert time.monotonic() - started < 5


_LONG_SECRET = "".join(
    random.Random(5000).choices(string.ascii_letters + string.digits + "-_.~:/@% \n", k=5000)
)
_BASIC = base64.b64encode(b"svc:p@ss/w:rd").decode()

_CHUNKING_CASES: dict[str, tuple[list[str], list[tuple[bytes, bytes]]]] = {
    "a strict prefix of another (the regression N1)": (
        ["hunter", "hunter2-prod", "ci:hunter", "ci:hunter2-prod", "nl\npw"],
        [
            (b"auth ci:hunter2-prod end\n", b"auth *** end\n"),
            (b"auth hunter2-prod, hunter and ci:hunter.\n", b"auth ***, *** and ***.\n"),
            (
                b"x nl\npw y https://ci:hunter2-prod@pkgs.example.org/simple/a.whl\n",
                b"x *** y https://***@pkgs.example.org/simple/a.whl\n",
            ),
            (b"hunter2-pro nl\np hunter2-prod", b"***2-pro nl\np ***"),
        ],
    ),
    "a prefix pair next to a secret with a line break": (
        ["abc", "abcdefgh", "x\ny"],
        [
            (b"tok abcdefgh end\n", b"tok *** end\n"),
            (b"tok abcdefg end abc x\ny\n", b"tok ***defg end *** ***\n"),
        ],
    ),
    "a suffix of another": (
        ["defgh", "abc\ndefgh"],
        [(b"p abc\ndefgh q defgh r abc\nzz\n", b"p *** q *** r abc\nzz\n")],
    ),
    "self-overlap": (
        ["aaa", "aaaaa"],
        [(b"b aaaaaaa b aa b aaaa\n", b"b *** b aa b ***\n")],
    ),
    "the end of one is the start of another": (
        ["abc", "cde"],
        [(b"xabcdey abcd cdeabc\n", b"x***y ***d ******\n")],
    ),
    "only a line break": (["\n"], [(b"ab\ncd\n\nef", b"ab***cd******ef")]),
    "one and two line breaks": (["\n", "\n\n"], [(b"ab\ncd\n\nef", b"ab***cd***ef")]),
    "one character": (["Z"], [(b"aZbZZc Z\n", b"a***b******c ***\n")]),
    "a secret with a line break inside a URL": (
        ["pa\nss"],
        [(b"get https://user:pa\nss@host/x\nnext pa\nss\n", b"get https://***@host/x\nnext ***\n")],
    ),
    "every form of an index credential": (
        [
            "p@ss/w:rd",
            "svc:p@ss/w:rd",
            "p%40ss%2Fw%3Ard",
            "svc:p%40ss%2Fw%3Ard",
            _BASIC,
            base64.b64encode(b"p@ss/w:rd").decode(),
        ],
        [
            (
                b"error: https://svc:p%40ss%2Fw%3Ard@pkgs.example.org/simple failed\n",
                b"error: https://***@pkgs.example.org/simple failed\n",
            ),
            (
                b"  Caused by: HTTP status client error (401 Unauthorized) for url\n"
                b"  (https://svc:p@ss/w:rd@pkgs.example.org/files/x.whl)\n",
                b"  Caused by: HTTP status client error (401 Unauthorized) for url\n"
                b"  (https://***@pkgs.example.org/files/x.whl)\n",
            ),
            (
                b"Authorization: Basic " + _BASIC.encode() + b"\npassword p@ss/w:rd.\n",
                b"Authorization: Basic ***\npassword ***.\n",
            ),
        ],
    ),
    "URLs with nothing listed": (
        [],
        [
            (
                b"GET http://other:unlisted@127.0.0.1:8080/x and http://host/a@b ok\n",
                b"GET http://***@127.0.0.1:8080/x and http://host/a@b ok\n",
            ),
            (b"a:b :/ x:// y ://@z ://u@ t://://v@w", b"a:b :/ x:// y ://@z ://***@ t://://***@w"),
            (b"see https://user:pw", b"see https://user:pw"),
            (
                "gr\u00fc\u00dfe \u2603 https://\u00fc:\u00df@h\u00f6st/\n".encode(),
                "gr\u00fc\u00dfe \u2603 https://***@h\u00f6st/\n".encode(),
            ),
        ],
    ),
    "a 5,000-character secret": (
        [_LONG_SECRET, "tail"],
        [
            (
                b"pre "
                + _LONG_SECRET.encode()
                + b" mid "
                + _LONG_SECRET.encode()[:4999]
                + b" tail\n",
                b"pre *** mid " + _LONG_SECRET.encode()[:4999] + b" ***\n",
            )
        ],
    ),
}


def _random_splits(text: bytes, count: int, seed: int) -> list[list[bytes]]:
    """Cut a text at random places, the same places on every run.

    Parameters
    ----------
    text
        The bytes to cut.
    count
        How many ways of cutting to return.
    seed
        Seed of the generator.

    Returns
    -------
    list of list of bytes
        `count` lists of reads; each joins to `text` and has three to nine reads.
    """
    rng = random.Random(seed)
    splits = []
    for _ in range(count):
        cuts = sorted(rng.randrange(len(text) + 1) for _ in range(rng.randint(2, 8)))
        edges = [0, *cuts, len(text)]
        splits.append([text[a:b] for a, b in itertools.pairwise(edges)])
    return splits


def _chunkings(text: bytes) -> list[list[bytes]]:
    """List the ways a text is fed to the redactor in the chunking tests.

    Parameters
    ----------
    text
        The bytes to cut.

    Returns
    -------
    list of list of bytes
        Every two-way split, a few hundred seeded multi-way splits (thirty for
        a long text) and, for a short text, one byte per read.
    """
    short = len(text) < 1000
    chunkings = [[text[:cut], text[cut:]] for cut in range(1, len(text))]
    chunkings += _random_splits(text, 300 if short else 30, seed=len(text))
    if short:
        chunkings.append([text[i : i + 1] for i in range(len(text))])
    return chunkings


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_one_read_gives_the_expected_redaction(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    for text, expected in texts:
        assert _collect(secrets, [text]) == expected


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_every_chunking_gives_the_output_of_one_read(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    forms = [secret.encode() for secret in secrets]
    for text, _expected in texts:
        single = _collect(secrets, [text])
        assert not any(form in single for form in forms)
        for chunks in _chunkings(text):
            assert b"".join(chunks) == text
            out = _collect(secrets, chunks)
            assert out == single, [len(chunk) for chunk in chunks]


@pytest.mark.parametrize("name", list(_CHUNKING_CASES))
def test_no_chunking_lets_a_secret_through_when_long_lines_are_flushed_early(name: str) -> None:
    secrets, texts = _CHUNKING_CASES[name]
    forms = [secret.encode() for secret in secrets]
    for text, _expected in texts:
        if len(text) >= 1000:
            continue
        for chunks in _chunkings(text):
            out = _collect(secrets, chunks, max_line=8)
            assert not any(form in out for form in forms), [len(chunk) for chunk in chunks]


def test_the_carry_over_is_bounded_by_the_longest_secret_and_the_longest_line() -> None:
    out: list[bytes] = []
    redactor = OutputRedactor(["aaa", "0123456789"], out.append, max_line=32)
    for chunk in (b"a" * 500, b"x 012345678", b"https://" + b"u" * 500, b":/", b"y\n" * 50):
        redactor.feed(chunk)
        assert len(redactor._buffer) <= max(32, len("0123456789") + 1)
    redactor.close()
    assert b"".join(out) == (b"***x 012345678https://" + b"u" * 500 + b":/" + b"y\n" * 50)


def test_capped_sink_forwards_up_to_the_limit_then_says_so_once() -> None:
    out: list[bytes] = []
    sink = CappedSink(out.append, limit=10)
    sink(b"12345")
    sink(b"6789012345")
    sink(b"more")
    sink(b"")
    assert b"".join(out) == b"1234567890\n[log truncated: output exceeded 10 bytes]\n"
    exact: list[bytes] = []
    fits = CappedSink(exact.append, limit=4)
    fits(b"1234")
    assert b"".join(exact) == b"1234"
    fits(b"5")
    assert b"".join(exact) == b"1234\n[log truncated: output exceeded 4 bytes]\n"


def test_is_private_directory(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    assert is_private_directory(private)
    assert is_private_directory(private, forbidden=0o077)
    private.chmod(0o750)
    assert is_private_directory(private)
    assert not is_private_directory(private, forbidden=0o077)
    private.chmod(0o770)
    assert not is_private_directory(private)
    private.chmod(0o707)
    assert not is_private_directory(private)
    assert not is_private_directory(tmp_path / "missing")
    plain = tmp_path / "file"
    plain.write_text("x")
    assert not is_private_directory(plain)
    link = tmp_path / "link"
    link.symlink_to(private)
    assert not is_private_directory(link)


def test_is_private_directory_refuses_another_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr("lakana_worker.processes.os.geteuid", lambda: 4242)
    assert not is_private_directory(private)


def _modes(root: Path) -> dict[str, int]:
    """Record every mode under a tree, without following links.

    Parameters
    ----------
    root
        The tree.

    Returns
    -------
    dict
        Relative path to permission bits.
    """
    found = {".": stat.S_IMODE(root.lstat().st_mode)}
    for directory, dirs, files in os.walk(root):
        for name in (*dirs, *files):
            path = Path(directory, name)
            found[str(path.relative_to(root))] = stat.S_IMODE(path.lstat().st_mode)
    return found


def test_remove_private_tree_removes_a_plain_tree(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    (tree / "home" / "deep").mkdir(parents=True)
    (tree / "home" / "deep" / "f").write_text("x")
    (tree / "result.json").write_text("{}")
    assert remove_private_tree(tree) is True
    assert not tree.exists()
    assert tmp_path.exists()


def test_remove_private_tree_on_a_missing_path_is_fine(tmp_path: Path) -> None:
    assert remove_private_tree(tmp_path / "missing") is True
    assert remove_private_tree(tmp_path / "missing" / "deeper") is True


def test_remove_private_tree_never_follows_a_root_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep")
    (outside / "sub").chmod(0o750)
    outside.chmod(0o755)
    before = _modes(outside)
    link = tmp_path / "code"
    link.symlink_to(outside)
    assert remove_private_tree(link) is True
    assert not link.is_symlink() and not link.exists()
    assert _modes(outside) == before
    assert (outside / "sub" / "keep.txt").read_text() == "keep"


def test_remove_private_tree_does_not_follow_a_nested_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep.txt").write_text("keep")
    outside.chmod(0o755)
    before = _modes(outside)
    tree = tmp_path / "attempt"
    (tree / "home").mkdir(parents=True)
    (tree / "home" / "dirlink").symlink_to(outside)
    (tree / "filelink").symlink_to(outside / "sub" / "keep.txt")
    (tree / "dangling").symlink_to(tmp_path / "nowhere")
    assert remove_private_tree(tree) is True
    assert not tree.exists()
    assert _modes(outside) == before
    assert (outside / "sub" / "keep.txt").read_text() == "keep"


@pytest.mark.parametrize("where", ["root", "sub", "nested"])
@pytest.mark.parametrize("mode", [0o000, 0o500, 0o100, 0o200])
def test_remove_private_tree_removes_directories_the_task_locked(
    tmp_path: Path, where: str, mode: int
) -> None:
    tree = tmp_path / "attempt"
    (tree / "sub" / "nested").mkdir(parents=True)
    (tree / "sub" / "nested" / "f").write_text("x")
    (tree / "sub" / "g").write_text("x")
    locked = {"root": tree, "sub": tree / "sub", "nested": tree / "sub" / "nested"}[where]
    locked.chmod(mode)
    if where == "nested":
        (tree / "sub").chmod(mode)
    try:
        assert remove_private_tree(tree) is True
    finally:
        if tree.exists():  # make pytest's own cleanup possible if the test fails
            for directory, _dirs, _files in os.walk(tree):
                os.chmod(directory, 0o700)
    assert not tree.exists()


def test_remove_private_tree_removes_special_files_and_locked_files(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    tree.mkdir()
    os.mkfifo(tree / "pipe")
    (tree / "locked").write_text("x")
    (tree / "locked").chmod(0o000)
    assert remove_private_tree(tree) is True
    assert not tree.exists()


def test_remove_private_tree_removes_a_read_only_tree(tmp_path: Path) -> None:
    tree = tmp_path / "attempt"
    (tree / "code" / "src").mkdir(parents=True)
    (tree / "code" / "src" / "a.py").write_text("x")
    (tree / "code" / "src" / "a.py").chmod(0o444)
    for directory in (tree / "code" / "src", tree / "code"):
        directory.chmod(0o555)
    assert remove_private_tree(tree) is True
    assert not tree.exists()


def test_remove_private_tree_removes_a_plain_file_at_the_path(tmp_path: Path) -> None:
    leftover = tmp_path / "attempt"
    leftover.write_text("x")
    assert remove_private_tree(leftover) is True
    assert not leftover.exists()


def test_remove_private_tree_reports_what_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "attempt"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "f").write_text("x")
    real_unlink = os.unlink

    def unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == "f":
            raise PermissionError(1, "operation not permitted")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.unlink", unlink)
    with caplog.at_level(logging.WARNING):
        assert remove_private_tree(tree) is False
    assert tree.exists()
    assert str(tree) in caplog.text


def test_remove_private_tree_refuses_a_path_without_a_name(tmp_path: Path) -> None:
    assert remove_private_tree(Path("/")) is False


def test_remove_private_tree_gives_up_on_a_chain_too_deep_for_its_descriptors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tree = tmp_path / "attempt"
    tree.mkdir()
    descriptor = os.open(tree, os.O_RDONLY)
    try:
        for _ in range(300):
            os.mkdir("d", dir_fd=descriptor)
            child = os.open("d", os.O_RDONLY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    with caplog.at_level(logging.WARNING):
        assert remove_private_tree(tree) is False
    assert tree.exists()
    assert str(tree) in caplog.text


def _fail_when(
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    when: Callable[..., bool],
    error: OSError,
) -> None:
    """Make one `os` function, as seen by `lakana_worker.processes`, fail on chosen calls.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    function
        The `os` function's name.
    when
        Receives the call's arguments; true means fail.
    error
        What to raise.
    """
    real = getattr(os, function)

    def wrapper(*args: object, **kwargs: object) -> object:
        if when(*args, **kwargs):
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(f"lakana_worker.processes.os.{function}", wrapper)


def _tree_with(tmp_path: Path, *names: str) -> Path:
    """Build a small tree.

    Parameters
    ----------
    tmp_path
        Where to build it.
    *names
        Relative file paths to create.

    Returns
    -------
    Path
        The tree's root.
    """
    tree = tmp_path / "attempt"
    tree.mkdir()
    for name in names:
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        (tree / name).write_text("x")
    return tree


def test_a_directory_swapped_for_a_link_after_the_listing_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    before = _modes(outside)
    tree = _tree_with(tmp_path)
    (tree / "swapped").symlink_to(outside)
    real_lstat = os.lstat

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if path == "swapped":  # the racing task: it was a directory when listed
            return real_lstat(outside)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.lstat", lstat)
    assert remove_private_tree(tree) is False
    assert _modes(outside) == before
    assert (outside / "keep").read_text() == "keep"


_O_PATH_ONLY = pytest.mark.skipif(
    not hasattr(os, "O_PATH"),
    reason="O_PATH is Linux-only; the macOS branch is tested with the O_PATH-less tests below",
)


@_O_PATH_ONLY
def test_a_locked_directory_that_cannot_be_opened_by_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    (tree / "sub").chmod(0)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: bool(a[1] & getattr(os, "O_PATH", 0)),
        OSError(errno.EBADF, "bad"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        (tree / "sub").chmod(0o700)


@_O_PATH_ONLY
def test_a_locked_root_that_cannot_be_opened_by_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "f")
    tree.chmod(0)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: bool(a[1] & getattr(os, "O_PATH", 0)),
        OSError(errno.EBADF, "bad"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        tree.chmod(0o700)


def test_a_locked_entry_that_is_not_a_directory_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "pretender")
    (tree / "pretender").chmod(0)
    real_lstat = os.lstat

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if path == "pretender":  # swapped for a file after the listing
            return real_lstat(tree)
        return real_lstat(path, dir_fd=dir_fd)

    monkeypatch.setattr("lakana_worker.processes.os.lstat", lstat)
    _fail_when(
        monkeypatch,
        "open",
        lambda *a, **k: a[0] == "pretender" and not a[1] & getattr(os, "O_PATH", 0),
        PermissionError(errno.EACCES, "denied"),
    )
    assert remove_private_tree(tree) is False
    assert (tree / "pretender").exists()


def test_a_chmod_that_fails_on_a_locked_directory_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    (tree / "sub").chmod(0)
    _fail_when(
        monkeypatch,
        "chmod",
        # The restore's `chmod`, by descriptor (Linux) or by name below the parent (macOS).
        lambda path, *a, **k: str(path) == "sub" or str(path).startswith("/proc/self/fd/"),
        OSError(errno.EPERM, "denied"),
    )
    try:
        assert remove_private_tree(tree) is False
    finally:
        (tree / "sub").chmod(0o700)


def _locked_tree(tmp_path: Path) -> Path:
    """Build a tree whose subdirectory ``sub`` the task set to mode 000.

    Parameters
    ----------
    tmp_path
        Where to build it.

    Returns
    -------
    Path
        The tree's root.
    """
    tree = _tree_with(tmp_path, "sub/f")
    (tree / "sub").chmod(0)
    return tree


@pytest.mark.usefixtures("without_o_path")
@pytest.mark.parametrize("where", ["root", "sub", "nested"])
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o200])
def test_without_o_path_directories_the_task_locked_are_removed(
    tmp_path: Path, where: str, mode: int
) -> None:
    """Without `O_PATH` (macOS) the restore is a `chmod` relative to the parent's descriptor."""
    tree = tmp_path / "attempt"
    (tree / "sub" / "nested").mkdir(parents=True)
    (tree / "sub" / "nested" / "f").write_text("x")
    locked = {"root": tree, "sub": tree / "sub", "nested": tree / "sub" / "nested"}[where]
    locked.chmod(mode)
    if where == "nested":
        (tree / "sub").chmod(mode)
    try:
        assert remove_private_tree(tree) is True
    finally:
        if tree.exists():
            for directory, _dirs, _files in os.walk(tree):
                os.chmod(directory, 0o700)
    assert not tree.exists()


@pytest.mark.usefixtures("without_o_path")
def test_without_o_path_a_directory_swapped_for_a_link_before_the_chmod_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: the task swaps in a link after the check; the `chmod` never reaches its target.

    The `lstat` saw a directory; by the `chmod` the name is a link to a directory
    outside. ``follow_symlinks=False`` changes at most the link itself (macOS) or
    refuses (Linux), and the reopen with ``O_NOFOLLOW`` refuses the link.
    """
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    before = _modes(outside)
    tree = _tree_with(tmp_path)
    (tree / "swapped").symlink_to(outside)
    real_lstat = os.lstat
    real_open = os.open

    def lstat(path: str, *, dir_fd: int | None = None) -> os.stat_result:
        if path == "swapped":  # it was a directory when the task listed it
            return real_lstat(outside)
        return real_lstat(path, dir_fd=dir_fd)

    def opening(path: str, flags: int, *args: int, **kwargs: int) -> int:
        if path == "swapped":  # a mode-000 directory refuses its owner
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("lakana_worker.processes.os.lstat", lstat)
    monkeypatch.setattr("lakana_worker.processes.os.open", opening)
    assert remove_private_tree(tree) is False
    assert _modes(outside) == before


@pytest.mark.parametrize("o_path", [True, False], ids=["o-path", "without-o-path"])
def test_a_locked_directory_of_another_owner_is_not_chmoded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    o_path: bool,
) -> None:
    """Only a directory of the worker's own user is made accessible again, on either branch."""
    if o_path and not hasattr(os, "O_PATH"):
        pytest.skip("O_PATH is Linux-only")
    if not o_path:
        request.getfixturevalue("without_o_path")
    tree = _locked_tree(tmp_path)
    monkeypatch.setattr("lakana_worker.processes.os.geteuid", lambda: os.getuid() + 4242)
    try:
        assert remove_private_tree(tree) is False
        assert stat.S_IMODE((tree / "sub").lstat().st_mode) == 0
    finally:
        (tree / "sub").chmod(0o700)


@pytest.mark.usefixtures("without_o_path")
@pytest.mark.parametrize(
    "error",
    [OSError(errno.EIO, "io"), FileNotFoundError(errno.ENOENT, "gone")],
    ids=["io", "vanished"],
)
def test_without_o_path_a_locked_directory_that_cannot_be_looked_at_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    """The check before the `chmod` fails: nothing is changed and the tree is reported."""
    tree = _locked_tree(tmp_path)
    looks: list[str] = []

    def second_look(path: str, **_kwargs: object) -> bool:
        """Pass the walk's own `lstat` of ``sub``, fail the restore's.

        Parameters
        ----------
        path
            The name looked at.
        **_kwargs
            The call's keyword arguments.

        Returns
        -------
        bool
            Whether this call fails.
        """
        if path != "sub":
            return False
        looks.append(path)
        return len(looks) == 2

    _fail_when(monkeypatch, "lstat", second_look, error)
    try:
        assert remove_private_tree(tree) is False
        assert stat.S_IMODE((tree / "sub").lstat().st_mode) == 0
    finally:
        (tree / "sub").chmod(0o700)


@pytest.mark.usefixtures("without_o_path")
@pytest.mark.parametrize(
    "error",
    [OSError(errno.EPERM, "denied"), FileNotFoundError(errno.ENOENT, "gone")],
    ids=["refused", "vanished"],
)
def test_without_o_path_a_chmod_that_fails_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    tree = _locked_tree(tmp_path)
    _fail_when(monkeypatch, "chmod", lambda path, *a, **k: path == "sub", error)
    try:
        assert remove_private_tree(tree) is False
    finally:
        (tree / "sub").chmod(0o700)


def test_where_no_safe_chmod_exists_a_locked_directory_is_left_and_the_reason_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither `O_PATH` nor a `chmod` that refuses links: never fall back to a `chmod` by path."""
    monkeypatch.delattr(os, "O_PATH", raising=False)
    monkeypatch.setattr("lakana_worker.processes._CHMOD_AT_WITHOUT_FOLLOWING", False)
    tree = _locked_tree(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            assert remove_private_tree(tree) is False
            assert remove_private_tree(tree) is False  # a second sweep says it no more
        assert stat.S_IMODE((tree / "sub").lstat().st_mode) == 0
    finally:
        (tree / "sub").chmod(0o700)
    refusals = [m for m in caplog.messages if "cannot be made accessible safely" in m]
    assert len(refusals) == 1
    assert str(tree / "sub") in refusals[0]


@pytest.mark.parametrize("o_path", [True, False], ids=["o-path", "without-o-path"])
def test_restoring_a_directory_that_is_gone_says_so(
    tmp_path: Path, request: pytest.FixtureRequest, o_path: bool
) -> None:
    """A vanished name is `FileNotFoundError` on either branch; the sweep skips it silently."""
    if o_path and not hasattr(os, "O_PATH"):
        pytest.skip("O_PATH is Linux-only")
    if not o_path:
        request.getfixturevalue("without_o_path")
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileNotFoundError):
            restore_directory_access("gone", parent, tmp_path / "gone")
    finally:
        os.close(parent)


def test_the_platform_check_matches_what_os_reports() -> None:
    """The fallback is enabled exactly where `os.chmod` takes a descriptor and refuses links."""
    from lakana_worker import processes

    assert processes._CHMOD_AT_WITHOUT_FOLLOWING is (
        os.chmod in os.supports_dir_fd and os.chmod in os.supports_follow_symlinks
    )


def test_an_entry_that_vanishes_during_the_walk_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "gone")
    _fail_when(
        monkeypatch,
        "lstat",
        lambda path, **k: path == "gone",
        FileNotFoundError(errno.ENOENT, "gone"),
    )
    assert remove_private_tree(tree) is False


def test_a_subdirectory_that_cannot_be_listed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree_with(tmp_path, "sub/f")
    calls: list[int] = []

    def when(*_args: object, **_kwargs: object) -> bool:
        calls.append(1)
        return len(calls) == 2  # the root's own chmod passes, the child's fails

    _fail_when(monkeypatch, "fchmod", when, OSError(errno.EIO, "io"))
    assert remove_private_tree(tree) is False


def test_an_unreadable_parent_is_reported(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    (parent / "attempt").mkdir(parents=True)
    parent.chmod(0)
    try:
        assert remove_private_tree(parent / "attempt") is False
    finally:
        parent.chmod(0o700)


def test_a_root_that_cannot_be_unlinked_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leftover = tmp_path / "attempt"
    leftover.write_text("x")
    _fail_when(monkeypatch, "unlink", lambda *a, **k: True, PermissionError(errno.EPERM, "no"))
    assert remove_private_tree(leftover) is False


def test_remove_private_tree_removes_a_sealed_input_copy_and_a_killed_download(
    tmp_path: Path,
) -> None:
    """Point 12: files 0400 and directories 0500 below `inputs`, and a partial leftover."""
    tree = tmp_path / "attempt"
    sealed = tree / "inputs" / "model"
    (sealed / "weights" / "deep").mkdir(parents=True)
    (sealed / "weights" / "deep" / "w.bin").write_text("x")
    (sealed / "config.json").write_text("{}")
    leftover = tree / "inputs" / ".model.lakana-partial-ab12cd"
    (leftover / "half").mkdir(parents=True)
    (leftover / "half" / "part").write_text("x")
    (tree / "output").mkdir(mode=0o700)
    (tree / "output" / "m.bin").write_text("x")
    for directory, _dirs, files in os.walk(tree, topdown=False):
        for name in files:
            os.chmod(os.path.join(directory, name), 0o400)
        os.chmod(directory, 0o500)
    try:
        assert remove_private_tree(tree) is True
    finally:
        if tree.exists():  # make pytest's own cleanup possible if the test fails
            for directory, _dirs, _files in os.walk(tree):
                os.chmod(directory, 0o700)
    assert not tree.exists()


def test_prepare_private_directory_creates_it_and_its_parents_with_mode_0700(
    tmp_path: Path,
) -> None:
    """A missing directory and the missing parents above it are made private."""
    target = tmp_path / "cache" / "attempts"
    prepare_private_directory(target)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(target.parent).st_mode) == 0o700
    assert is_private_directory(target, 0o077)


def test_prepare_private_directory_tolerates_a_directory_that_exists(tmp_path: Path) -> None:
    """The second of two workers that start together finds the directory and keeps it."""
    target = tmp_path / "attempts"
    prepare_private_directory(target)
    (target / "attempt-1").mkdir()
    prepare_private_directory(target)
    assert (target / "attempt-1").is_dir()


def test_prepare_private_directory_tolerates_losing_the_race_to_create_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another worker creates the directory between the check and the `mkdir`."""
    target = tmp_path / "attempts"
    real_mkdir = os.mkdir

    def rival_wins(path: str | os.PathLike[str], mode: int = 0o777) -> None:
        """Create the directory as the other worker would, then fail as `mkdir` does.

        Parameters
        ----------
        path
            The directory to make.
        mode
            Its mode.

        Raises
        ------
        FileExistsError
            Always, after making the directory.
        """
        real_mkdir(path, mode)
        raise FileExistsError(errno.EEXIST, "File exists", str(path))

    monkeypatch.setattr(os, "mkdir", rival_wins)
    prepare_private_directory(target)
    assert is_private_directory(target, 0o077)


def test_prepare_private_directory_refuses_a_symlink_in_its_place(tmp_path: Path) -> None:
    """A link to a directory is not the directory; the message names the path and the fix."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    target = tmp_path / "attempts"
    target.symlink_to(elsewhere)
    with pytest.raises(UnsafeDirectory, match=r"attempts.*real directory.*0700"):
        prepare_private_directory(target)


def test_prepare_private_directory_refuses_a_directory_others_can_use(tmp_path: Path) -> None:
    """An existing directory with group or other access is refused, not silently changed."""
    target = tmp_path / "attempts"
    target.mkdir()
    target.chmod(0o755)
    with pytest.raises(UnsafeDirectory):
        prepare_private_directory(target)
    assert stat.S_IMODE(os.lstat(target).st_mode) == 0o755


# macOS: a task can lock its own files with user flags (`chflags uchg`, `uappnd`) or an ACL entry
# that denies their deletion (#166). Linux has neither for a non-root user, so the tests below
# replace macOS at its boundary: `_FakeMacLocks` stands in for the libc calls that clear a lock,
# and `os.unlink`, `os.rmdir` and `os.fchmod` refuse a locked inode as macOS's kernel does. The
# last tests run the real calls, on macOS only.


def _key(info: os.stat_result) -> tuple[int, int]:
    """Name an inode.

    Parameters
    ----------
    info
        Its status.

    Returns
    -------
    tuple of int
        Device and inode number.
    """
    return (info.st_dev, info.st_ino)


class _FakeMacLocks:
    """The locks a task set, by inode, and the calls that clear them through a descriptor."""

    def __init__(self) -> None:
        """Start with nothing locked."""
        self.flagged: set[tuple[int, int]] = set()
        self.denied: set[tuple[int, int]] = set()
        self.touched: list[tuple[int, int]] = []

    def lock(self, path: Path, how: str) -> None:
        """Lock one entry, as the task would.

        Parameters
        ----------
        path
            The entry; a link is locked itself, not its target.
        how
            ``flags`` (`chflags uchg`) or ``acl`` (`chmod +a "everyone deny delete"`, or
            ``delete_child`` on a directory).
        """
        (self.flagged if how == "flags" else self.denied).add(_key(path.lstat()))

    def clear_flags(self, fd: int, flags: int) -> None:
        """Clear the flags of the inode behind `fd`.

        Parameters
        ----------
        fd
            An open descriptor.
        flags
            Its current flags, unused: Linux has none to read.
        """
        key = _key(os.fstat(fd))
        self.touched.append(key)
        self.flagged.discard(key)

    def strip_acl(self, fd: int) -> None:
        """Remove the ACL of the inode behind `fd`.

        Parameters
        ----------
        fd
            An open descriptor.
        """
        key = _key(os.fstat(fd))
        self.touched.append(key)
        self.denied.discard(key)


@pytest.fixture
def mac_locks(monkeypatch: pytest.MonkeyPatch) -> _FakeMacLocks:
    """Run the cleanup as on macOS, with a kernel that enforces the fake's locks.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.

    Returns
    -------
    _FakeMacLocks
        The locks, for the test to set and inspect.
    """
    locks = _FakeMacLocks()
    monkeypatch.setattr("lakana_worker.processes._DARWIN", True)
    monkeypatch.setattr("lakana_worker.processes._mac_locks", locks)
    real_unlink, real_rmdir, real_fchmod = os.unlink, os.rmdir, os.fchmod

    def refuse(name: str, dir_fd: int | None) -> None:
        target = _key(os.lstat(name, dir_fd=dir_fd))
        parent = _key(os.fstat(dir_fd)) if dir_fd is not None else None
        if target in locks.flagged | locks.denied or parent in locks.flagged | locks.denied:
            raise PermissionError(errno.EPERM, "Operation not permitted", name)

    def unlink(name: str, *, dir_fd: int | None = None) -> None:
        refuse(name, dir_fd)
        real_unlink(name, dir_fd=dir_fd)

    def rmdir(name: str, *, dir_fd: int | None = None) -> None:
        refuse(name, dir_fd)
        real_rmdir(name, dir_fd=dir_fd)

    def fchmod(fd: int, mode: int) -> None:
        if _key(os.fstat(fd)) in locks.flagged:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_fchmod(fd, mode)

    monkeypatch.setattr("lakana_worker.processes.os.unlink", unlink)
    monkeypatch.setattr("lakana_worker.processes.os.rmdir", rmdir)
    monkeypatch.setattr("lakana_worker.processes.os.fchmod", fchmod)
    return locks


@pytest.mark.parametrize("how", ["flags", "acl"])
@pytest.mark.parametrize("where", ["root", "sub", "sub/f", "sub/nested/g", "fifo"])
def test_on_macos_a_tree_the_task_locked_is_removed(
    tmp_path: Path, mac_locks: _FakeMacLocks, where: str, how: str
) -> None:
    """A locked file or directory anywhere in the tree is unlocked, then removed (#166)."""
    tree = _tree_with(tmp_path, "sub/f", "sub/nested/g")
    os.mkfifo(tree / "fifo")
    mac_locks.lock(tree if where == "root" else tree / where, how)

    assert remove_private_tree(tree) is True
    assert not tree.exists()


def test_without_macos_a_locked_file_is_left_and_nothing_is_unlocked(
    tmp_path: Path, mac_locks: _FakeMacLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlocking runs on macOS only: elsewhere the kernel has no such locks to clear."""
    monkeypatch.setattr("lakana_worker.processes._DARWIN", False)
    tree = _tree_with(tmp_path, "f")
    mac_locks.lock(tree / "f", "flags")

    assert remove_private_tree(tree) is False
    assert (tree / "f").exists()
    assert mac_locks.touched == []


def test_on_macos_a_locked_hard_link_is_left_and_the_file_outside_untouched(
    tmp_path: Path, mac_locks: _FakeMacLocks, caplog: pytest.LogCaptureFixture
) -> None:
    """A hard link shares the inode of a file outside the tree: unlocking it would unlock that.

    The tree stays; the operator gets the path and the manual fix, once per path.
    """
    outside = tmp_path / "outside"
    outside.write_text("the worker's own file")
    tree = _tree_with(tmp_path, "f")
    os.link(outside, tree / "linked")
    mac_locks.lock(outside, "flags")

    with caplog.at_level(logging.WARNING):
        assert remove_private_tree(tree) is False
        assert remove_private_tree(tree) is False
    assert _key(outside.lstat()) not in mac_locks.touched
    assert _key(outside.lstat()) in mac_locks.flagged
    assert (tree / "linked").exists()
    assert not (tree / "f").exists()
    fixes = [m for m in caplog.messages if "chflags -R nouchg,nouappnd" in m]
    assert len(fixes) == 1
    assert str(tree) in fixes[0]


def test_on_macos_a_link_to_a_locked_file_outside_is_unlinked_and_its_target_untouched(
    tmp_path: Path, mac_locks: _FakeMacLocks
) -> None:
    """A symlink is removed as itself; the lock of what it points to is never cleared."""
    outside = tmp_path / "outside"
    outside.write_text("the worker's own file")
    tree = _tree_with(tmp_path, "f")
    (tree / "link").symlink_to(outside)
    mac_locks.lock(outside, "flags")
    mac_locks.lock(tree / "f", "flags")

    assert remove_private_tree(tree) is True
    assert _key(outside.lstat()) not in mac_locks.touched
    assert _key(outside.lstat()) in mac_locks.flagged


def test_on_macos_a_locked_link_is_never_opened_through(
    tmp_path: Path, mac_locks: _FakeMacLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `O_SYMLINK` (Linux) a locked link cannot be opened as itself, so it is left."""
    monkeypatch.delattr(os, "O_SYMLINK", raising=False)
    outside = tmp_path / "outside"
    outside.write_text("the worker's own file")
    tree = _tree_with(tmp_path)
    (tree / "link").symlink_to(outside)
    mac_locks.lock(tree / "link", "flags")

    assert remove_private_tree(tree) is False
    assert (tree / "link").is_symlink()
    assert _key(outside.lstat()) not in mac_locks.touched


def test_on_macos_a_lock_that_cannot_be_cleared_leaves_the_entry(
    tmp_path: Path, mac_locks: _FakeMacLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing libc call (a file system without ACLs, a denied right) is not fatal."""

    def refuse(fd: int, *args: object) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(mac_locks, "clear_flags", refuse)
    monkeypatch.setattr(mac_locks, "strip_acl", refuse)
    tree = _tree_with(tmp_path, "f")
    mac_locks.lock(tree / "f", "acl")

    assert remove_private_tree(tree) is False
    assert (tree / "f").exists()


def test_on_macos_an_entry_of_another_user_is_never_unlocked(
    tmp_path: Path, mac_locks: _FakeMacLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the worker's own files are unlocked: a root worker meets others' only on NFS."""
    monkeypatch.setattr("lakana_worker.processes.os.geteuid", lambda: os.getuid() + 1)
    tree = _tree_with(tmp_path, "f")
    mac_locks.lock(tree / "f", "flags")

    assert remove_private_tree(tree) is False
    assert mac_locks.touched == []


def test_on_macos_an_entry_still_refused_after_unlocking_is_left(
    tmp_path: Path, mac_locks: _FakeMacLocks, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calls succeed but the kernel still refuses (a lock the calls do not cover)."""
    monkeypatch.setattr(mac_locks, "clear_flags", lambda fd, flags: None)
    monkeypatch.setattr(mac_locks, "strip_acl", lambda fd: None)
    tree = _tree_with(tmp_path, "f")
    mac_locks.lock(tree / "f", "flags")

    assert remove_private_tree(tree) is False
    assert (tree / "f").exists()


class _FakeFunction:
    """One C function of `_FakeLibc`: records its calls and takes the declared signature."""

    def __init__(self, calls: list[tuple[object, ...]], name: str, result: object) -> None:
        """Prepare the function.

        Parameters
        ----------
        calls
            The list every call is appended to.
        name
            The function's name.
        result
            What each call returns.
        """
        self.calls, self.name, self.result = calls, name, result
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        """Record the call.

        Parameters
        ----------
        *args
            The arguments.

        Returns
        -------
        object
            The result chosen at construction.
        """
        self.calls.append((self.name, *args))
        return self.result


class _FakeLibc:
    """macOS's libc as `_MacLocks` calls it: records each call, fails where told."""

    def __init__(self, fail: str = "") -> None:
        """Prepare the functions.

        Parameters
        ----------
        fail
            The name of the call that returns failure, if any.
        """
        self.calls: list[tuple[object, ...]] = []
        results = {"fchflags": (0, -1), "acl_init": (4096, None), "acl_set_fd_np": (0, -1)}
        results["acl_free"] = (0, -1)
        for name, (ok, failed) in results.items():
            setattr(self, name, _FakeFunction(self.calls, name, failed if name == fail else ok))


def _locks_over(libc: _FakeLibc) -> object:
    """Build the real `_MacLocks` over a fake library.

    Parameters
    ----------
    libc
        The fake.

    Returns
    -------
    object
        The `_MacLocks`.
    """
    from lakana_worker.processes import _MacLocks

    return _MacLocks(lambda: cast("ctypes.CDLL", libc))


def test_the_libc_calls_clear_only_the_lock_flags_and_set_an_empty_acl() -> None:
    """`fchflags` keeps every other flag; the ACL is replaced by an empty extended one."""
    from lakana_worker.processes import _MacLocks

    libc = _FakeLibc()
    locks = cast("_MacLocks", _locks_over(libc))
    hidden = stat.UF_HIDDEN
    locks.clear_flags(7, stat.UF_IMMUTABLE | stat.UF_APPEND | hidden)
    locks.clear_flags(7, hidden)  # nothing locked: no call
    locks.strip_acl(7)

    assert libc.calls == [
        ("fchflags", 7, hidden),
        ("acl_init", 1),
        ("acl_set_fd_np", 7, 4096, 0x100),
        ("acl_free", 4096),
    ]
    fchflags = cast("_FakeFunction", vars(libc)["fchflags"])
    assert fchflags.restype is not None  # the signatures are declared on first use


@pytest.mark.parametrize("fail", ["fchflags", "acl_init", "acl_set_fd_np"])
def test_a_failing_libc_call_raises_an_os_error(fail: str) -> None:
    """The caller decides what a failure means; an ACL made is freed either way."""
    from lakana_worker.processes import _MacLocks

    libc = _FakeLibc(fail)
    locks = cast("_MacLocks", _locks_over(libc))
    with pytest.raises(OSError, match=fail):
        locks.clear_flags(7, stat.UF_IMMUTABLE)
        locks.strip_acl(7)
    assert (("acl_free", 4096) in libc.calls) is (fail == "acl_set_fd_np")


_DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin", reason="user flags and ACLs exist on macOS only"
)


def _unlock_by_hand(path: Path) -> None:
    """Clear what a failed test may have left locked, so pytest can remove its directory.

    Parameters
    ----------
    path
        A tree or file; missing is fine.
    """
    if os.path.lexists(path):
        subprocess.run(["/usr/bin/chflags", "-R", "nouchg,nouappnd", str(path)], check=False)
        subprocess.run(["/bin/chmod", "-R", "-N", str(path)], check=False)


@_DARWIN_ONLY
@pytest.mark.parametrize("flag", ["uchg", "uappnd"])
def test_on_real_macos_flags_on_files_and_directories_are_cleared(
    tmp_path: Path, flag: str
) -> None:
    """The real `fchflags` through ctypes: a locked file in a locked directory goes."""
    tree = _tree_with(tmp_path, "sub/f")
    try:
        subprocess.run(["/usr/bin/chflags", flag, str(tree / "sub" / "f")], check=True)
        subprocess.run(["/usr/bin/chflags", flag, str(tree / "sub")], check=True)
        assert remove_private_tree(tree) is True
        assert not tree.exists()
    finally:
        _unlock_by_hand(tree)


@_DARWIN_ONLY
def test_on_real_macos_a_deny_delete_acl_is_stripped(tmp_path: Path) -> None:
    """The real `acl_set_fd_np`: deny-delete on a file and deny-delete_child on its directory."""
    tree = _tree_with(tmp_path, "sub/f")
    try:
        subprocess.run(
            ["/bin/chmod", "+a", "everyone deny delete", str(tree / "sub" / "f")], check=True
        )
        subprocess.run(
            ["/bin/chmod", "+a", "everyone deny delete_child", str(tree / "sub")], check=True
        )
        assert remove_private_tree(tree) is True
        assert not tree.exists()
    finally:
        _unlock_by_hand(tree)


@_DARWIN_ONLY
def test_on_real_macos_a_locked_file_outside_stays_locked(tmp_path: Path) -> None:
    """Neither a symlink nor a hard link in the tree unlocks the file outside it."""
    outside = tmp_path / "outside"
    outside.write_text("x")
    tree = _tree_with(tmp_path)
    (tree / "symlink").symlink_to(outside)
    os.link(outside, tree / "hardlink")
    try:
        subprocess.run(["/usr/bin/chflags", "uchg", str(outside)], check=True)
        assert remove_private_tree(tree) is False
        assert outside.lstat().st_flags & stat.UF_IMMUTABLE  # ty: ignore[missing-attribute]
        assert not (tree / "symlink").exists() and (tree / "hardlink").exists()
    finally:
        _unlock_by_hand(outside)
        _unlock_by_hand(tree)
