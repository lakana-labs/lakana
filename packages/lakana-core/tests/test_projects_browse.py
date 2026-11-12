"""The link to a deployed file on its git host (`lakana_core.projects.browse`)."""

from __future__ import annotations

import pytest

from lakana_core.models import RepositoryKind
from lakana_core.projects.browse import browse_url

SHA = "0123456789abcdef0123456789abcdef01234567"


def link(
    url: str, kind: RepositoryKind = RepositoryKind.HTTPS, path: str = "assets/gold/f.sql"
) -> str | None:
    """Build the link to line 1 of `path` at `SHA`.

    Parameters
    ----------
    url
        The stored repository URL.
    kind
        The repository's kind.
    path
        The file's path in the repository.

    Returns
    -------
    str | None
        What `browse_url` answers.
    """
    return browse_url(kind, url, commit=SHA, path=path, line=1)


def test_browse_url_strips_userinfo() -> None:
    """Credentials in a stored URL never reach the link."""
    assert link("https://tok:x@github.com/acme/an.git") == (
        f"https://github.com/acme/an/blob/{SHA}/assets/gold/f.sql#L1"
    )
    assert link("https://org@dev.azure.com/org/proj/_git/an") == (
        f"https://dev.azure.com/org/proj/_git/an?path=/assets/gold/f.sql&version=GC{SHA}"
        "&line=1&lineEnd=1&lineStartColumn=1"
    )


def test_browse_url_converts_ssh_to_https() -> None:
    """An SSH URL, ``ssh://`` or scp-style, becomes the host's HTTPS page."""
    s = RepositoryKind.SSH
    assert link("git@gitlab.com:grp/sub/an.git", s) == (
        f"https://gitlab.com/grp/sub/an/-/blob/{SHA}/assets/gold/f.sql#L1"
    )
    assert (link("ssh://git@ssh.github.com:443/acme/an.git", s) or "").startswith(
        "https://github.com/acme/an/blob/"
    )
    assert (link("git@ssh.dev.azure.com:v3/org/proj/an", s) or "").startswith(
        "https://dev.azure.com/org/proj/_git/an?"
    )
    assert link("git@codeberg.org:o/r.git", s) == (
        f"https://codeberg.org/o/r/src/commit/{SHA}/assets/gold/f.sql#L1"
    )
    assert link("git@bitbucket.org:w/r.git", s) == (
        f"https://bitbucket.org/w/r/src/{SHA}/assets/gold/f.sql#lines-1"
    )


def test_gitea_and_an_absolute_scp_path_get_a_link() -> None:
    """Gitea uses the Forgejo pattern; an scp path may start with ``/``."""
    assert link("https://gitea.com/o/r") == (
        f"https://gitea.com/o/r/src/commit/{SHA}/assets/gold/f.sql#L1"
    )
    assert (link("git@github.com:/o/r.git", RepositoryKind.SSH) or "").startswith(
        "https://github.com/o/r/blob/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com.evil.example/a/b",
        "https://evilvisualstudio.com/p/_git/r",
        "https://github.com\n/a/b",
        "https://bitbuc\u212aet.org/a/b",
        "git@vs-ssh.visualstudio.com:v3/o/p/r",
        "https://github.com/acme/.git",
        "https://github.com/-/r",
        "https://github.com/acme/-r",
        "https://www.github.com/a/b",
        "https://github.com:8443/a/b",
        "https://git%68ub.com/a/b",
        "https://github.com./a/b",
        "https:///a/b",
    ],
)
def test_a_lookalike_host_gets_no_link(url: str) -> None:
    """Only the five public hosts, matched whole, get a link."""
    kind = RepositoryKind.SSH if url.startswith("git@") else RepositoryKind.HTTPS
    assert link(url, kind) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/../r",
        "https://github.com/./r",
        "https://github.com/a/%2e%2e",
        "https://github.com/a/..git",
        "https://gitlab.com/a//r",
        "https://dev.azure.com/org/my project/_git/r",
        "https://github.com/a/b/c",
        "https://github.com/a",
        "https://gitlab.com/r",
    ],
)
def test_a_dot_segment_in_the_repository_path_gets_no_link(url: str) -> None:
    """The repository path's grammar refuses dot, empty and odd segments, and wrong counts."""
    assert link(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://bitbuc\u212aet.org/a/b",
        "https://git\u212aub.com/a/b",
        "git@bitbuc\u212aet.org:a/b.git",
        "ssh://git@bitbuc\u212aet.org/a/b",
        "https://user@githu\u0431.com/a/b",
        "git@bitbuc\u212aet.org://a/b",
    ],
)
def test_a_kelvin_sign_host_gets_no_link_and_never_raises(url: str) -> None:
    """A non-ASCII authority is refused before anything lower-cases or NFKC-checks it."""
    kind = RepositoryKind.HTTPS if url.startswith("https") else RepositoryKind.SSH
    assert link(url, kind) is None


def test_a_url_urlsplit_refuses_gets_no_link() -> None:
    """A malformed authority or port gives no link instead of an error."""
    assert link("https://[github.com/a/b") is None
    assert link("https://github.com:99999/a/b") is None
    assert link("ssh://git@github.com:x/o/r", RepositoryKind.SSH) is None


def test_a_local_repository_has_no_link_and_no_path() -> None:
    """A local repository's path never leaves the server."""
    assert link("/srv/git/analytics", RepositoryKind.LOCAL) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/a/b\u200b",
        "https://github.com/a/b\t",
        "https://github.com/a/b?x=1",
        "https://github.com/a/b#frag",
        "http://github.com/a/b",
    ],
)
def test_a_url_with_hidden_text_a_query_or_plain_http_gets_no_link(url: str) -> None:
    """Format and control characters, whitespace, ``?``, ``#`` and ``http`` give no link."""
    assert link(url) is None


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (RepositoryKind.SSH, "git@github.com"),
        (RepositoryKind.SSH, "https://github.com/a/b"),
        (RepositoryKind.SSH, "git@ssh.dev.azure.com:org/proj/an"),
        (RepositoryKind.SSH, "git@ssh.dev.azure.com:v3/org/an"),
        (RepositoryKind.HTTPS, "https://dev.azure.com/org/proj/an/x"),
        (RepositoryKind.HTTPS, "https://dev.azure.com/org/proj/an"),
        (RepositoryKind.HTTPS, "https://org.visualstudio.com/proj/an"),
        (RepositoryKind.HTTPS, "https://org.visualstudio.com/an"),
        (RepositoryKind.HTTPS, "https://org.visualstudio.com/Other/proj/_git/an"),
    ],
)
def test_a_url_that_does_not_fit_its_host_gets_no_link(kind: RepositoryKind, url: str) -> None:
    """An scp URL without a path, a wrong scheme, and an Azure path of the wrong shape."""
    assert link(url, kind) is None


def test_a_commit_that_is_not_a_full_sha_gets_no_link() -> None:
    """Only a full, lower-case 40-hex commit goes into a link."""
    url = "https://github.com/a/b"
    for commit in ("main", SHA.upper(), SHA[:39], f"{SHA}\n"):
        assert browse_url(RepositoryKind.HTTPS, url, commit=commit, path="f.sql", line=1) is None


def test_browse_url_normalises_case_trailing_slash_and_port() -> None:
    """URLs as people type them still give the right page."""
    assert link("https://GitHub.com/Acme/An.git/") == (
        f"https://github.com/Acme/An/blob/{SHA}/assets/gold/f.sql#L1"
    )
    assert (link("https://gitlab.com:443/g/r") or "").startswith("https://gitlab.com/g/r/-/blob/")
    assert (link("ssh://git@github.com:22/o/r.git", RepositoryKind.SSH) or "").startswith(
        "https://github.com/o/r/blob/"
    )
    assert (link("https://org.visualstudio.com/proj/_git/an") or "").startswith(
        "https://org.visualstudio.com/proj/_git/an?path="
    )
    assert (link("https://org.visualstudio.com/DefaultCollection/proj/_git/an") or "").startswith(
        "https://org.visualstudio.com/DefaultCollection/proj/_git/an?path=/assets/gold/f.sql&"
    )


def test_every_path_segment_is_percent_encoded() -> None:
    """Dot segments, spaces, ``%``, ``#`` and ``?`` in the file path cannot move the link."""
    assert link("https://github.com/a/b", path="assets/../x y/%2e.sql") == (
        f"https://github.com/a/b/blob/{SHA}/assets/%2E%2E/x%20y/%252e.sql#L1"
    )
    assert link("https://github.com/a/b", path="./a#b?.sql") == (
        f"https://github.com/a/b/blob/{SHA}/%2E/a%23b%3F.sql#L1"
    )
    assert (link("https://dev.azure.com/o/p/_git/r", path="a&line=9.sql") or "").startswith(
        f"https://dev.azure.com/o/p/_git/r?path=/a%26line%3D9.sql&version=GC{SHA}&"
    )
