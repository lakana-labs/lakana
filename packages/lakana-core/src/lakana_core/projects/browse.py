"""The link to a deployed file on its git host ("Defined in").

`browse_url` turns a stored repository URL, a commit, a file path and a line into an HTTPS
link to that line on the repository's web host. The server returns it, so the CLI and the
interface show the same link. It is built only for five public hosts, GitHub, GitLab,
Bitbucket, Codeberg or Gitea, and Azure DevOps, and only from parts that fit each host's
grammar. Anything else, including a ``local`` repository, gets no link: the commit and the path
stay visible, and a wrong link is never better than none.

The stored URL may be older than today's validation, so nothing in it is trusted:

- a control, format or whitespace character, ``?`` or ``#`` anywhere gives no link;
- the authority must be ASCII *before* anything lower-cases it, since Unicode case folding
  turns the Kelvin sign into ``k`` and so a look-alike host into a real one;
- userinfo is dropped, SSH becomes HTTPS (its port is dropped), and an HTTPS port other than
  443 gives no link;
- every host test is a `re.fullmatch`, and every repository segment fits
  ``[A-Za-z0-9._-]+``, is neither ``.`` nor ``..`` and does not start with ``-``.

The file path is percent-encoded segment by segment, with ``.`` and ``..`` written as ``%2E``
and ``%2E%2E``, so a forged path cannot steer a browser to another page of the host.

This module imports nothing from `lakana_core.runs` or `lakana_core.catalog.service`.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote, urlsplit

from lakana_core.models import RepositoryKind

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_HOST = re.compile(
    r"github\.com|gitlab\.com|bitbucket\.org|codeberg\.org|gitea\.com|dev\.azure\.com"
    r"|[a-z0-9-]+\.visualstudio\.com"
)
_SSH_HOSTS = {"ssh.github.com": "github.com", "ssh.dev.azure.com": "dev.azure.com"}
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_SCP = re.compile(r"(?:[^@/:]+@)?(?P<host>[^@/:]+):(?P<path>.*)")
_AZURE = "{base}?path=/{path}&version=GC{sha}&line={n}&lineEnd={n}&lineStartColumn=1"
_PATTERNS = {
    "github.com": "{base}/blob/{sha}/{path}#L{n}",
    "gitlab.com": "{base}/-/blob/{sha}/{path}#L{n}",
    "bitbucket.org": "{base}/src/{sha}/{path}#lines-{n}",
    "codeberg.org": "{base}/src/commit/{sha}/{path}#L{n}",
    "gitea.com": "{base}/src/commit/{sha}/{path}#L{n}",
}


def browse_url(kind: RepositoryKind, url: str, *, commit: str, path: str, line: int) -> str | None:
    """Build the HTTPS link to a line of a file at a commit, on the repository's git host.

    Parameters
    ----------
    kind
        The repository's kind; ``local`` never gets a link.
    url
        The stored repository URL, ``https://…``, ``ssh://…`` or scp-style
        ``[user@]host:path``.
    commit
        The deployed commit, a full lower-case 40-hex SHA.
    path
        The file's path in the repository, ``/``-separated.
    line
        The line to point at.

    Returns
    -------
    str | None
        The link, or None when the host, the URL or the commit does not fit a known host.
    """
    if kind is RepositoryKind.LOCAL or not _FULL_SHA.fullmatch(commit) or not _plain(url):
        return None
    located = _locate(kind, url)
    if located is None:
        return None
    host, segments = located
    repository = _repository_path(kind, host, segments)
    if repository is None:
        return None
    file_path = "/".join(_segment(s) for s in path.split("/"))
    pattern = _PATTERNS.get(host, _AZURE)
    return pattern.format(base=f"https://{host}/{repository}", sha=commit, path=file_path, n=line)


def _plain(url: str) -> bool:
    """Report whether a URL is free of hidden text, whitespace, ``?`` and ``#``.

    Parameters
    ----------
    url
        The stored repository URL.

    Returns
    -------
    bool
        False when it holds a ``Cc`` or ``Cf`` character, whitespace, ``?`` or ``#``, which
        `urlsplit` would drop or read as the end of the authority.
    """
    return not any(c in "?#" or c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in url)


def _locate(kind: RepositoryKind, url: str) -> tuple[str, list[str]] | None:
    """Split a URL into its web host and its repository path segments.

    Parameters
    ----------
    kind
        The repository's kind, ``https`` or ``ssh``.
    url
        A URL `_plain` accepted.

    Returns
    -------
    tuple[str, list[str]] | None
        The lower-case web host and the path's segments with one trailing ``.git`` removed,
        or None when the URL does not parse, its host is not a known one or a segment does
        not fit the grammar.
    """
    with_scheme = kind is RepositoryKind.HTTPS or url.startswith("ssh://")
    authority = url.partition("://")[2].split("/", 1)[0] if with_scheme else url.split(":", 1)[0]
    if not authority.isascii():
        return None
    if with_scheme:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        wanted = "https" if kind is RepositoryKind.HTTPS else "ssh"
        if parts.scheme != wanted or (wanted == "https" and port not in (None, 443)):
            return None
        host, repository = parts.hostname or "", parts.path
    else:
        scp = _SCP.fullmatch(url)
        if scp is None:
            return None
        host, repository = scp["host"], scp["path"]
    host = host.lower()
    if host == "vs-ssh.visualstudio.com":
        return None
    host = _SSH_HOSTS.get(host, host)
    if not _HOST.fullmatch(host):
        return None
    segments = repository.removeprefix("/").removesuffix("/").split("/")
    segments[-1] = segments[-1].removesuffix(".git")
    if not all(_SEGMENT.fullmatch(s) and s not in (".", "..") and s[0] != "-" for s in segments):
        return None
    return host, segments


def _repository_path(kind: RepositoryKind, host: str, segments: list[str]) -> str | None:
    """Give the repository's web path on its host, or None when the segments do not fit it.

    Parameters
    ----------
    kind
        The repository's kind, ``https`` or ``ssh``.
    host
        The web host `_locate` matched.
    segments
        The repository path's segments.

    Returns
    -------
    str | None
        ``owner/repo``, ``group/…/repo``, ``org/project/_git/repo`` or
        ``[DefaultCollection/]project/_git/repo``.
    """
    count = len(segments)
    if host == "dev.azure.com" and kind is RepositoryKind.SSH:
        if count != 4 or segments[0] != "v3":
            return None
        _, org, project, repo = segments
        return f"{org}/{project}/_git/{repo}"
    if host == "dev.azure.com":
        fits = count == 4 and segments[2] == "_git"
    elif host.endswith(".visualstudio.com"):
        fits = count in (3, 4) and segments[-2] == "_git"
        fits = fits and (count == 3 or segments[0] == "DefaultCollection")
    elif host == "gitlab.com":
        fits = count >= 2
    else:
        fits = count == 2
    return "/".join(segments) if fits else None


def _segment(segment: str) -> str:
    """Percent-encode one segment of a file path.

    Parameters
    ----------
    segment
        One ``/``-separated part of the stored path.

    Returns
    -------
    str
        ``%2E`` for ``.``, ``%2E%2E`` for ``..``, else the segment with every reserved
        character encoded.
    """
    if segment in (".", ".."):
        return "%2E" * len(segment)
    return quote(segment, safe="")
