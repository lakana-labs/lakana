"""`lakana artifact *`: store versioned files such as models next to tables."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from lakana._artifact_manifest import shown
from lakana._models import (
    ArtifactKept,
    ArtifactUpload,
    ArtifactVersion,
    DeletedArtifactVersion,
)
from lakana.cli import _output
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._paging import CURSOR, LIMIT, more_results
from lakana.cli._state import CliError, confirm, resolve_environment, state_of

artifact_app = typer.Typer(
    help="Manage artifacts: versioned files such as models.", no_args_is_help=True
)

versions_app = typer.Typer(
    help=(
        "Show the versions of an artifact: when each was made, its size, its file count, its "
        "digest, and whether it was deleted."
    ),
    no_args_is_help=True,
)
artifact_app.add_typer(versions_app, name="versions")

uploads_app = typer.Typer(
    help=(
        "List and withdraw uploads that were begun and not finished. `lakana artifact upload` "
        "withdraws its own when it fails; use `list` to find one a killed process left behind "
        "and `abort` to withdraw it."
    ),
    no_args_is_help=True,
)
artifact_app.add_typer(uploads_app, name="uploads")

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_DIGEST_SHOWN = 12


def human_size(size: int) -> str:
    """Render a byte count for people.

    Parameters
    ----------
    size
        Bytes.

    Returns
    -------
    str
        ``19 B``, ``1.5 KiB``, ``2.0 MiB`` or ``3.2 GiB``.
    """
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KiB", "MiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _files(count: int) -> str:
    """Write a number of files.

    Parameters
    ----------
    count
        The number.

    Returns
    -------
    str
        ``1 file`` or ``2 files``.
    """
    return f"{count} file" if count == 1 else f"{count} files"


def upload_summary(environment: str, name: str, version: ArtifactVersion) -> str:
    """Say what an upload stored, for someone who has never used the command.

    Parameters
    ----------
    environment
        The environment.
    name
        The artifact's dotted name.
    version
        What the upload returned.

    Returns
    -------
    str
        One line, and a second one when the upload knows how many files it sent. The version
        number is the server's: numbers can have gaps.
    """
    text = (
        f"Stored {_output.inline(environment)}/{_output.inline(name)} version {version.version} "
        f"({_files(version.file_count)}, {human_size(version.size_bytes)})"
    )
    if version.files_sent is not None and version.files_reused is not None:
        reused = (
            f"{version.files_reused} already stored"
            if version.files_reused
            else "none were already stored"
        )
        text += f"\nSent {_files(version.files_sent)}; {reused}."
    if version.deleted_versions:
        text += (
            "\nPruned old versions: " + ", ".join(str(n) for n in version.deleted_versions) + "."
        )
    return text


@artifact_app.command("upload")
def upload(
    ctx: typer.Context,
    name: str,
    path: Path,
    env: _ENV = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Store PATH (a file or a directory) as a new version of artifact NAME (namespace.name).

    Everything in a directory is stored, including hidden files such as .env: point the command
    at a directory that holds only the artifact. Only files the store does not have yet are
    sent. Links, devices and Windows-unsafe names are refused before anything is sent; the
    message names the path.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    path
        The file or directory to store.
    env
        The environment.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    version = state.client().artifacts.upload(environment, name, path, break_glass_reason=glass)
    _output.done(state, upload_summary(environment, name, version), version)


@dataclass(frozen=True)
class _ArtifactRow:
    """One line of `lakana artifact list`.

    Parameters
    ----------
    name
        The artifact's name.
    namespace
        Its namespace.
    current_version
        The highest live version, if any.
    versions
        How many versions it has had.
    size
        The size of its live versions, for people.
    keep
        How many live versions the declaring project keeps; empty keeps all.
    defined_in
        ``repository/path`` of the declaring file; empty when no deployed project declares it.
    """

    name: str
    namespace: str
    current_version: int | None
    versions: int
    size: str
    keep: str
    defined_in: str


@dataclass(frozen=True)
class _VersionRow:
    """One line of `lakana artifact versions list`.

    Parameters
    ----------
    version
        The number; numbers can have gaps.
    created_at
        When it was committed.
    size
        Its size, for people.
    file_count
        How many files it has.
    manifest_sha256
        The start of its manifest digest; the whole digest is in `--output json`.
    run_id
        The run that made it, if the server shows it to you.
    deleted_at
        When it was deleted, if it was.
    """

    version: int
    created_at: str
    size: str
    file_count: int
    manifest_sha256: str
    run_id: str | None
    deleted_at: str | None


@artifact_app.command("list")
def list_artifacts(
    ctx: typer.Context,
    namespace: Annotated[
        str | None, typer.Option("--namespace", help="Only this namespace, e.g. models.")
    ] = None,
    env: _ENV = None,
    limit: LIMIT = None,
    cursor: CURSOR = None,
) -> None:
    """List the artifacts you may read, one page at a time.

    Shows the current version, the size, how many versions a deployed project keeps (`keep`;
    empty means all) and the file that declares the artifact (`defined in`; empty for an
    artifact no deployed project declares). The last line names the command for the next page
    when there is one.

    Parameters
    ----------
    ctx
        The Typer context.
    namespace
        Only this namespace.
    env
        The environment.
    limit
        `--limit`.
    cursor
        `--cursor`.
    """
    state = state_of(ctx)
    page = state.client().artifacts.list_page(
        resolve_environment(state, env), namespace, limit=limit, cursor=cursor
    )
    if state.output == "json":
        _output.emit(state, page.items)
    else:
        rows = [
            _ArtifactRow(
                a.name,
                a.namespace,
                a.current_version,
                a.versions,
                human_size(a.size_bytes),
                "" if a.keep_versions is None else str(a.keep_versions),
                "" if a.defined_by is None else f"{a.defined_by.repository}/{a.defined_by.path}",
            )
            for a in page.items
        ]
        _output.emit(
            state,
            rows,
            columns=[
                "name",
                "namespace",
                "current_version",
                "versions",
                "size",
                "keep",
                "defined in",
            ],
            derived={"defined in": lambda row: row.defined_in},
            droppable=["defined in", "keep", "size", "versions"],
        )
    more_results(
        state,
        ["lakana", "artifact", "list"],
        {"--env": env, "--namespace": namespace, "--limit": limit},
        page.next_cursor,
    )


@versions_app.command("list")
def list_versions(ctx: typer.Context, name: str, env: _ENV = None) -> None:
    """List the versions of artifact NAME (namespace.name), newest first.

    A deleted version is listed too, with when it was deleted; its bytes are gone. Version
    numbers can have gaps. If an upload said the server did not confirm its commit, look here for
    a version with the manifest_sha256 it names.

    On a narrow terminal the table leaves out columns, the digest last, and says which; the
    JSON form always has them all and the whole digest: `lakana --output json artifact
    versions list NAME`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    artifacts = state.client().artifacts
    artifact = artifacts.get(environment, name)
    versions = artifacts.versions(environment, name)
    if state.output == "json":
        _output.print_json(_output.to_jsonable({"artifact": artifact, "versions": versions}))
        return
    rows = [
        _VersionRow(
            v.version,
            v.created_at,
            human_size(v.size_bytes),
            v.file_count,
            v.manifest_sha256[:_DIGEST_SHOWN],
            v.run_id,
            v.deleted_at,
        )
        for v in versions
    ]
    _output.emit(
        state,
        rows,
        columns=[
            "version",
            "created_at",
            "size",
            "file_count",
            "manifest_sha256",
            "run_id",
            "deleted_at",
        ],
        droppable=["file_count", "size", "run_id", "deleted_at", "manifest_sha256"],
    )
    live = sum(1 for v in versions if v.deleted_at is None)
    noun = "version" if live == 1 else "versions"
    _output.emit(state, f"Total: {live} {noun} with bytes, {human_size(artifact.size_bytes)}")


def _versions_word(count: int) -> str:
    """Write a number of versions.

    Parameters
    ----------
    count
        The number.

    Returns
    -------
    str
        ``1 version`` or ``2 versions``.
    """
    return f"{count} version" if count == 1 else f"{count} versions"


def _kept_clause(kept: ArtifactKept) -> str:
    """Say why a group of files stayed, in words that follow ``Kept``.

    Parameters
    ----------
    kept
        One group from the server's answer.

    Returns
    -------
    str
        For example ``2 files (300 B) that version 5 also uses``. A reason this client does not
        know reads as the clean-up's, which is true of anything the delete did not remove.
    """
    amount = f"{_files(kept.files)} ({human_size(kept.bytes)})"
    if kept.reason == "other_version":
        who = "another version" if kept.version is None else f"version {kept.version}"
        return f"{amount} that {who} also uses"
    if kept.reason == "open_upload":
        return f"{amount} that an open upload names"
    return (
        f"{amount} that Lakana's periodic clean-up decides about: it removes what nothing "
        "else needs and keeps the rest"
    )


def deletion_summary(environment: str, name: str, deleted: DeletedArtifactVersion) -> str:
    """Say what a delete did, for someone who wants to know whether storage was freed.

    Parameters
    ----------
    environment
        The environment.
    name
        The artifact's dotted name.
    deleted
        What the delete returned.

    Returns
    -------
    str
        One sentence for what was freed and, if any file stayed, a second for what and why. A
        server that does not report the counts gets a plain statement of that instead.
    """
    head = f"Deleted version {deleted.version} of {_output.inline(environment)}/{shown(name)}."
    if deleted.freed_files is None or deleted.freed_bytes is None:
        return (
            f"{head} This server does not say how many files it freed. A file another version "
            "or an open upload still needs stays; Lakana's periodic clean-up removes the rest."
        )
    freed = (
        f"{_files(deleted.freed_files)} ({human_size(deleted.freed_bytes)})"
        if deleted.freed_files
        else "no files"
    )
    text = f"{head} Freed {freed}."
    if deleted.kept:
        clauses = [_kept_clause(kept) for kept in deleted.kept]
        listed = (
            clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + ", and " + clauses[-1]
        )
        text += f"\nKept {listed}."
    return text


@versions_app.command("delete")
def delete_version(
    ctx: typer.Context,
    name: str,
    version: Annotated[
        int, typer.Argument(min=1, help="The version number, from `versions list`.")
    ],
    env: _ENV = None,
    yes: _YES = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Delete one version of artifact NAME (namespace.name). This cannot be undone.

    The version's files are removed from Lakana's storage, except a file another version still
    has. A bucket that keeps old object versions, and a backup, still have them. The
    version stays in `versions list`, marked deleted, with who deleted it and when; its number is
    never reused. The command says how many files it freed and how many it kept, and why: another
    version has them, an open upload names them, or Lakana's periodic clean-up decides about them
    (for example for a large version, a manifest the catalog does not know after a restore, a
    damaged manifest or a storage failure). Needs DROP_TABLE on the artifact, which
    a task never has. In a protected environment pass --break-glass --reason.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    version
        The version number.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    break_glass
        `--break-glass`.
    reason
        `--reason`.

    Raises
    ------
    CliError
        If the version is deleted already, or the user declines.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    artifacts = state.client().artifacts
    found = artifacts.version(environment, name, version)
    if found.deleted_at is not None:
        raise CliError(
            f"Version {version} of {environment}/{name} was already deleted ({found.deleted_at}). "
            f"Run `lakana artifact versions list {name} --env {environment}` to see the others."
        )
    confirm(
        state,
        f"Delete version {version} of {environment}/{name} "
        f"({human_size(found.size_bytes)}, {_files(found.file_count)})? "
        "Its bytes are removed from Lakana's storage. Lakana cannot bring them back.",
        yes,
    )
    deleted = artifacts.delete_version(environment, name, version, break_glass_reason=glass)
    _output.done(state, deletion_summary(environment, name, deleted), deleted)


@artifact_app.command("drop")
def drop(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    yes: _YES = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Drop artifact NAME (namespace.name): every version, its files and its grants.

    This cannot be undone. The name becomes free; a new artifact of that name starts at version
    1. The files are removed inside the request, so an artifact with thousands of files can take
    minutes; if the server fails part-way, Lakana's periodic clean-up finishes the job. Needs
    DROP_TABLE on the artifact, which a task never has. In a protected environment pass
    --break-glass --reason.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    artifacts = state.client().artifacts
    artifact = artifacts.get(environment, name)
    listed = artifacts.versions(environment, name)
    live = sum(1 for v in listed if v.deleted_at is None)
    earlier = len(listed) - live
    more = f", {earlier} more already deleted" if earlier else ""
    confirm(
        state,
        f"Drop artifact {environment}/{name} and delete its {_versions_word(live)} "
        f"({human_size(artifact.size_bytes)}{more})? This cannot be undone.",
        yes,
    )
    artifacts.drop(environment, name, break_glass_reason=glass)
    _output.done(
        state,
        f"Dropped artifact {_output.inline(environment)}/{_output.inline(name)}; its files are "
        "removed from storage, and Lakana's periodic clean-up removes any it could not.",
    )


@dataclass(frozen=True)
class _UploadRow:
    """One line of `lakana artifact uploads list`.

    Parameters
    ----------
    upload_id
        The id `uploads abort` needs.
    artifact
        The artifact, ``namespace.name``.
    created_at
        When the upload was begun.
    expires_at
        When it frees its place by itself.
    """

    upload_id: str
    artifact: str
    created_at: str
    expires_at: str


def _upload_row(upload: ArtifactUpload) -> _UploadRow:
    """Make a table row of an upload, with every server-given string made safe to print.

    Parameters
    ----------
    upload
        The upload as the server listed it.

    Returns
    -------
    _UploadRow
        The row; control characters are escaped and long values cut.
    """
    return _UploadRow(
        shown(upload.upload_id),
        shown(upload.artifact),
        shown(upload.created_at),
        shown(upload.expires_at),
    )


@uploads_app.command("list")
def list_uploads(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="Only this artifact (namespace.name); every artifact by default."),
    ] = None,
    env: _ENV = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Uploads per page.")] = 100,
    cursor: Annotated[
        str | None, typer.Option("--cursor", help="Continue from a previous page.")
    ] = None,
    all_pages: Annotated[bool, typer.Option("--all", help="Follow every page.")] = False,
) -> None:
    """List your own open uploads, newest first: the id, the artifact, when it began and expires.

    Only your own: another person's uploads are never listed. An upload that was begun and never
    finished (after a killed process, a lost connection or a power cut) holds one of its
    artifact's 16 upload places until it expires, one hour by default. Withdraw one with
    `lakana artifact uploads abort NAME UPLOAD_ID`. The list does not say how many files an
    upload has sent.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`; every artifact when left out.
    env
        The environment.
    limit
        Uploads per page.
    cursor
        A previous page's `next_cursor`.
    all_pages
        Follow `next_cursor` to the end.

    Raises
    ------
    CliError
        For `--cursor` with `--all`.
    """
    state = state_of(ctx)
    if all_pages and cursor:
        raise CliError("--cursor and --all can't be combined")
    environment = resolve_environment(state, env)
    artifacts = state.client().artifacts
    if all_pages:
        uploads = list(artifacts.iter_uploads(environment, name, page_size=limit))
        next_cursor = None
    else:
        page = artifacts.list_uploads(environment, name, limit=limit, cursor=cursor)
        if state.output == "json":
            _output.print_json(page)
            return
        uploads, next_cursor = page.items, page.next_cursor
    if state.output == "json":
        _output.print_json(_output.to_jsonable(uploads))
        return
    if not uploads:
        _output.emit(state, f"You have no open uploads in {_output.inline(environment)}.")
        return
    columns = ["upload_id", "artifact", "created_at", "expires_at"]
    _output.emit(
        state,
        [_upload_row(u) for u in uploads],
        columns=columns,
        droppable=["expires_at", "created_at"],
    )
    _output.emit(
        state,
        "Withdraw one with `lakana artifact uploads abort NAME UPLOAD_ID --env "
        f"{shown(environment)}`.",
    )
    if next_cursor:
        sys.stdout.write(f"More: --cursor {shown(next_cursor)}\n")


@uploads_app.command("abort")
def abort_upload(ctx: typer.Context, name: str, upload_id: str, env: _ENV = None) -> None:
    """Withdraw upload UPLOAD_ID of artifact NAME (namespace.name) that was never finished.

    `lakana artifact upload` does this by itself when it fails or is interrupted, so you rarely
    need it. An artifact allows 16 open uploads at once; an abandoned one frees its place when
    it is withdrawn or after the upload lifetime (one hour by default). Only the person (or
    service account) that began the upload can withdraw it, and only while it is open. Files it
    already stored stay until Lakana's periodic clean-up finds that nothing needs them.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    upload_id
        The upload's id.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    state.client().artifacts.abort_upload(environment, name, upload_id)
    _output.done(
        state,
        f"Withdrew upload {upload_id} of {_output.inline(environment)}/{_output.inline(name)}",
    )


@artifact_app.command("download")
def download(
    ctx: typer.Context,
    name: str,
    directory: Path,
    version: Annotated[
        int | None,
        typer.Option(
            "--version", min=1, help="The version to download (default: the current one)."
        ),
    ] = None,
    env: _ENV = None,
) -> None:
    """Download a version of artifact NAME (namespace.name) into DIRECTORY.

    DIRECTORY must not exist, or be empty. The files appear there only when all of them have
    arrived and match the manifest; if anything goes wrong nothing is left at DIRECTORY (a
    directory that did not exist is not there, one that existed is empty again) and the message
    says what to do. An empty directory is never replaced: it keeps its place and its mode, and
    the files are moved into it at the end. Nothing is written through a link.

    The copy is private to you, because the artifact's contents are access-controlled on the
    server: files are 0600 and directories 0700 (less your umask), nothing is executable. To open
    the copy to others run `chmod -R a+rX DIR`. (A read-only copy, files 0400 and directories
    0500, is what a task gets as its input; remove one with `chmod -R u+w DIR` first.)

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The qualified artifact name, such as `models.churn`.
    directory
        Where to write: a new or an empty directory.
    version
        The version number; the current version by default.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    got = state.client().artifacts.download(environment, name, directory, version=version)
    _output.done(
        state,
        f"Downloaded {_output.inline(environment)}/{_output.inline(name)} version {got.version} "
        f"to {_output.inline(str(got.path))}",
        got,
    )


def register(app: typer.Typer) -> None:
    """Add the `artifact` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(artifact_app, name="artifact")
