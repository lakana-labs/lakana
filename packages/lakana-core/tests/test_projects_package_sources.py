"""Which package sources a lock may name: URL rules and the known set."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from lakana_core.errors import ValidationError
from lakana_core.projects.package_sources import (
    PYPI_URL,
    KnownSources,
    RegisteredIndex,
    comparable_index_url,
    normalize_index_url,
    without_userinfo,
)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("https://pkgs.example.org/simple", "https://pkgs.example.org/simple"),
        ("https://Pkgs.Example.org:443/simple/", "https://pkgs.example.org/simple"),
        ("HTTPS://pkgs.example.org/simple//", "https://pkgs.example.org/simple"),
        ("https://pkgs.example.org:8443/a/B/simple", "https://pkgs.example.org:8443/a/B/simple"),
        ("https://pkgs.example.org", "https://pkgs.example.org"),
        ("http://127.0.0.1:8080/simple", "http://127.0.0.1:8080/simple"),
        ("http://localhost:80/simple", "http://localhost/simple"),
        ("http://[::1]:8080/simple", "http://[::1]:8080/simple"),
    ],
)
def test_spellings_of_one_url_are_one_index(given: str, stored: str) -> None:
    """Scheme, host and default port are folded; the path keeps its case."""
    assert normalize_index_url(given, credential=True) == stored
    assert normalize_index_url(given, credential=False) == stored
    assert comparable_index_url(given) == stored


@pytest.mark.parametrize(
    ("url", "part_of_the_message"),
    [
        ("https://user:hunter2@pkgs.example.org/simple", "must not contain a user name"),
        ("https://token@pkgs.example.org/simple", "must not contain a user name"),
        ("https://pkgs.example.org/simple?token=hunter2", "must not contain '?' or '#'"),
        ("https://pkgs.example.org/simple#frag", "must not contain '?' or '#'"),
        ("http://pkgs.example.org/simple", "must use https"),
        ("http://127.0.0.1.example.org/simple", "must use https"),
        ("ftp://pkgs.example.org/simple", "must start with https://"),
        ("pkgs.example.org/simple", "must start with https://"),
        ("https:///simple", "must start with https://"),
        ("https://pkgs.example.org:port/simple", "is malformed"),
        ("https://[::1/simple", "is malformed"),
        ("https://pkgs.example.org/sim ple", "whitespace, control characters"),
        ("https://pkgs.example.org/simple\n", "whitespace, control characters"),
        ("https://pkgs.example.org/" + "a" * 1024, "longer than 1024 characters"),
    ],
)
def test_a_url_that_cannot_be_registered_is_refused_without_being_quoted(
    url: str, part_of_the_message: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        normalize_index_url(url, credential=False)
    with pytest.raises(ValidationError) as with_credential:
        normalize_index_url(url, credential=True)
    assert str(with_credential.value) == str(caught.value)
    assert part_of_the_message in str(caught.value)
    assert "hunter2" not in str(caught.value)
    assert "example.org" not in str(caught.value).replace("pkgs.example.org/simple.", "")


@pytest.mark.parametrize(
    "url",
    [
        PYPI_URL,
        "https://PyPI.org:443/simple/",
        "https://pypi.org/other",
        "https://pypi.org",
        "https://files.pythonhosted.org/packages",
    ],
)
def test_the_public_index_cannot_be_registered_so_it_never_gets_a_credential(url: str) -> None:
    for credential in (False, True):
        with pytest.raises(ValidationError) as caught:
            normalize_index_url(url, credential=credential)
        assert str(caught.value) == (
            "The package index URL is PyPI's own address. PyPI takes no credential and needs no "
            "registration: a lock may take packages from https://pypi.org/simple as it is."
        )
    assert KnownSources((), None).is_known(PYPI_URL)


def test_a_url_with_userinfo_a_query_or_no_host_equals_no_known_source() -> None:
    for url in (
        "https://u:p@pkgs.example.org/simple",
        "https://pkgs.example.org/simple?x=1",
        "https://pkgs.example.org/simple#x",
        "file:///srv/wheels",
        "wheelhouse",
        "https://pkgs.example.org:port/simple",
    ):
        assert comparable_index_url(url) is None


def test_userinfo_is_dropped_from_a_setting_before_it_is_compared() -> None:
    assert without_userinfo("https://op:pw@mirror.corp/simple/") == "https://mirror.corp/simple/"
    assert without_userinfo("https://mirror.corp/simple") == "https://mirror.corp/simple"
    assert without_userinfo("https://[::1/simple") == "https://[::1/simple"


def test_the_known_sources() -> None:
    team = RegisteredIndex("team", "https://pkgs.example.org/simple", "team-index")
    plain = RegisteredIndex("open", "https://open.example.org/simple")
    sources = KnownSources((team, plain), "https://op:pw@Mirror.corp/simple/")
    for known in (
        PYPI_URL,
        "https://PyPI.org/simple/",
        "https://pkgs.example.org/simple/",
        "https://open.example.org:443/simple",
        "https://mirror.corp/simple",
        "https://op:pw@mirror.corp/simple",
    ):
        assert sources.is_known(known), known
    for unknown in ("https://evil.example/simple", "https://mirror.corp/other", "wheelhouse", ""):
        assert not sources.is_known(unknown), unknown
    assert sources.by_url() == {team.url: team, plain.url: plain}
    assert "pw" not in repr(sources)
    assert KnownSources().is_known(PYPI_URL)
    assert not KnownSources(operator_index="not a url").is_known("not a url")


@pytest.mark.parametrize(
    ("operator", "lock"),
    [
        ("http://mirror.corp/simple", "http://mirror.corp/simple"),
        ("http://10.0.0.5:8080/simple", "http://10.0.0.5:8080/simple/"),
        ("https://op:pw@devpi.corp/root/pypi/+simple/", "https://devpi.corp/root/pypi/+simple"),
        ("https://pypi_mirror.corp/simple", "https://PYPI_mirror.corp/simple"),
        (
            "https://host.corp/AbCdEf0123456789%2Bxyz/simple/",
            "https://HOST.corp:443/AbCdEf0123456789%2Bxyz/simple",
        ),
        ("https://mirror.corp/simple", "https://u:p@mirror.corp/simple"),
        ("http://[::1]:8080/simple", "http://[::1]:8080/simple"),
    ],
)
def test_the_operators_index_stays_known_as_it_was(operator: str, lock: str) -> None:
    """Review N1: the setting is validated elsewhere; the strict rule is for registered indexes."""
    sources = KnownSources(operator_index=operator)
    assert sources.is_known(lock)
    assert "pw" not in repr(sources)


@pytest.mark.parametrize(
    "lock",
    [
        "https://other.corp/simple",
        "https://mirror.corp:8443/simple",
        "http://mirror.corp/simple",
        "https://mirror.corp/other",
        "https://mirror.corp/simple/x",
        "https://mirror.corp/a%2Bb/simple",
        "https://mirror.corp.evil.example/simple",
    ],
)
def test_a_lock_url_that_differs_from_the_operators_in_more_than_form_is_not_known(
    lock: str,
) -> None:
    assert not KnownSources(operator_index="https://op:pw@mirror.corp/a+b/simple/").is_known(lock)


@pytest.mark.parametrize(
    "text", ["", " ", "http://[", "https://h:99999999/x", "\ud800", "a" * 5000]
)
def test_neither_side_of_the_operators_comparison_raises(text: str) -> None:
    assert not KnownSources(operator_index=text).is_known("https://mirror.corp/simple")
    assert not KnownSources(operator_index="https://mirror.corp/simple").is_known(text)


def test_a_registered_index_still_needs_the_strict_form() -> None:
    """The lenient comparison is the operator's alone."""
    sources = KnownSources((RegisteredIndex("x", "https://pkgs.example.org/simple"),))
    for lock in (
        "https://pkgs.example.org/a/../simple",
        "https://pkgs.example.org\\.evil.org/simple",
        "http://pkgs.example.org/simple",
        "https://u:p@pkgs.example.org/simple",
        "https://pkgs.example.org/simple?x=1",
    ):
        assert not sources.is_known(lock), lock


def test_a_stored_row_in_a_non_normalised_form_does_not_make_the_lock_url_known() -> None:
    row = RegisteredIndex("x", "https://Pkgs.Example.org:443/simple/")
    sources = KnownSources((row,))
    assert not sources.is_known("https://Pkgs.Example.org:443/simple/")
    assert not sources.is_known("https://pkgs.example.org/simple")


@pytest.mark.parametrize(
    ("url", "part_of_the_message"),
    [
        # The whole URL is printable ASCII without a backslash.
        ("https://evil.example\\.good.example/simple", "whitespace, control characters"),
        ("https://example.org/simple\u00a0x", "whitespace, control characters"),
        ("https://example.org/simple\u2028x", "whitespace, control characters"),
        ("https://example.org/simple\u3000x", "whitespace, control characters"),
        ("https://example.org/simple\x85x", "whitespace, control characters"),
        ("https://pkgs\u00a0.example.org/simple", "whitespace, control characters"),
        ("https://\uff45\uff58\uff41.org/simple", "xn--"),
        ("https://b\u00fccher.example/simple", "xn--"),
        ("https://ex\u00adample.org/simple", "xn--"),
        ("https://example.org/\ud800/simple", "xn--"),
        ("https://example.org/a\\b/simple", "backslash"),
        # Hosts: ASCII DNS names, or an IP literal in its one canonical spelling.
        ("https://ex%61mple.org/simple", "host"),
        ("https://example.org%2f.evil.com/simple", "host"),
        ("https://%00/simple", "host"),
        ("https://./simple", "host"),
        ("https://example.org./simple", "trailing dot"),
        ("https://a..example.org/simple", "empty label"),
        ("https://-a.example.org/simple", "host"),
        ("https://a-.example.org/simple", "host"),
        ("https://ex,ample.org/simple", "host"),
        ("https://" + "a" * 64 + ".example.org/simple", "63"),
        ("https://2130706433/simple", "IPv4"),
        ("https://0x7f.1/simple", "IPv4"),
        ("https://127.1/simple", "IPv4"),
        ("https://0177.0.0.1/simple", "IPv4"),
        ("https://1.2.3.256/simple", "IPv4"),
        ("https://[0:0:0:0:0:0:0:1]/simple", "[::1]"),
        ("https://[::0001]/simple", "[::1]"),
        ("https://[::ffff:127.0.0.1]/simple", "write the IPv4 address"),
        ("https://[::ffff:7f00:1]/simple", "write the IPv4 address"),
        ("https://[::FFFF:192.0.2.1]:8443/simple", "write the IPv4 address"),
        ("https://[fe80::1%eth0]/simple", "zone"),
        ("https://[fe80::1%25eth0]/simple", "zone"),
        ("https://[v1.x]/simple", "IPv6"),
        # Paths: URL-safe segments, no empty one, no dot segment in any spelling.
        ("https://example.org/a/../simple", "'.' or '..'"),
        ("https://example.org/a/./simple", "'.' or '..'"),
        ("https://example.org/a/%2e%2e/simple", "percent sign or a space"),
        ("https://example.org/a/%2E/simple", "percent sign or a space"),
        ("https://example.org/a%20b/simple", "percent sign or a space"),
        ("https://example.org//simple", "empty"),
        ("https://example.org/a//simple", "empty"),
        ("https://example.org/{x}/simple", "letters, digits"),
        ("https://example.org/\u00e9/simple", "xn--"),
        ("https://example.org/a;b/simple", "letters, digits"),
        # A port is between 1 and 65535.
        ("https://example.org:0/simple", "between 1 and 65535"),
        ("http://localhost:0/simple", "between 1 and 65535"),
    ],
)
def test_a_url_uv_would_read_differently_is_refused(url: str, part_of_the_message: str) -> None:
    """Review I1-I3: a stored URL means one origin to Python and to uv, in one spelling."""
    with pytest.raises(ValidationError) as caught:
        normalize_index_url(url, credential=False)
    assert part_of_the_message in str(caught.value)
    assert comparable_index_url(url) is None
    assert not KnownSources().is_known(url)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("https://[2001:DB8::1]:8443/simple", "https://[2001:db8::1]:8443/simple"),
        ("https://192.0.2.10/simple/", "https://192.0.2.10/simple"),
        ("https://xn--bcher-kva.example/simple", "https://xn--bcher-kva.example/simple"),
        ("https://example.org/", "https://example.org"),
        ("https://example.org//", "https://example.org"),
        # Trailing slashes of any number fold into the one stored form.
        ("http://localhost:8080/simple//", "http://localhost:8080/simple"),
        ("http://localhost:8080/simple/", "http://localhost:8080/simple"),
        ("https://xn--80ak6aa92e.example/simple", "https://xn--80ak6aa92e.example/simple"),
        # Lakana does not check that an xn-- label is valid (Python's codec follows IDNA 2003,
        # uv the newer rules); uv refuses an invalid one at build time, and a valid one that
        # the codec would refuse (ss/sigma) must register.
        ("https://xn--strae-oqa.de/simple", "https://xn--strae-oqa.de/simple"),
        ("https://xn--fa-hia.de/simple", "https://xn--fa-hia.de/simple"),
        ("https://xn--a.example/simple", "https://xn--a.example/simple"),
        ("https://example.org:65535/simple", "https://example.org:65535/simple"),
        ("https://example.org:1/simple", "https://example.org:1/simple"),
        (
            "https://my-host.example.org/a.b/c_d/e~f/simple",
            "https://my-host.example.org/a.b/c_d/e~f/simple",
        ),
        ("https://1host.example/simple", "https://1host.example/simple"),
        (
            "https://devpi.example.org/root/pypi/+simple/",
            "https://devpi.example.org/root/pypi/+simple",
        ),
        (
            "https://pkgs.example.org/o/_packaging/feed@Release/pypi/simple/",
            "https://pkgs.example.org/o/_packaging/feed@Release/pypi/simple",
        ),
    ],
)
def test_an_accepted_url_has_one_stored_form(given: str, stored: str) -> None:
    """Review I2: what is accepted is stored in the one form `comparable_index_url` gives."""
    assert normalize_index_url(given, credential=True) == stored == comparable_index_url(given)
    assert normalize_index_url(stored, credential=True) == stored


@pytest.mark.parametrize(
    "lock",
    [
        "https://evil.example\\@operator.example/simple",
        "https://evil.example:443\\@operator.example/simple",
        "https://oper\tator.example/simple",
        "https://operator.example/simple\n",
        "\thttps://operator.example/simple",
        "https://operator.example/simple ",
        "https://operat\u00f6r.example/simple",
        "https://operator.example/s\u00efmple",
    ],
)
def test_the_lenient_comparison_needs_printable_ascii_and_no_backslash(lock: str) -> None:
    """NB1: a URL uv reads as another host, or that Python repairs, is not the operator's index."""
    sources = KnownSources(operator_index="https://op:hunter2@operator.example/simple/")
    assert not sources.is_known(lock)
    assert sources.is_known("https://x:y@operator.example/simple")


@pytest.mark.parametrize(
    "host",
    [
        "pypi.org",
        "www.pypi.org",
        "upload.pypi.org",
        "pypi.python.org",
        "files.pythonhosted.org",
        "test-files.pythonhosted.org",
    ],
)
def test_six_hosts_of_the_public_index_are_refused_with_and_without_a_credential(host: str) -> None:
    for credential in (False, True):
        with pytest.raises(ValidationError, match="PyPI's own address"):
            normalize_index_url(f"https://{host}/simple", credential=credential)


@pytest.mark.parametrize(
    "url",
    [
        "https://test.pypi.org/simple",
        "https://Test.PyPI.org:443/simple/",
        "https://test.pypi.org:8443/simple",
        "https://test.pypi.org",
    ],
)
def test_testpypi_may_be_registered_but_never_with_a_credential(url: str) -> None:
    stored = normalize_index_url(url, credential=False)
    assert stored == comparable_index_url(url)
    parts = urlsplit(stored)
    assert (parts.scheme, parts.hostname) == ("https", "test.pypi.org")
    with pytest.raises(ValidationError) as caught:
        normalize_index_url(url, credential=True)
    assert str(caught.value) == (
        "The package index URL is TestPyPI's address. TestPyPI needs no credential for "
        "downloads; register it without one (leave out --credential-secret)."
    )
    with pytest.raises(ValidationError):
        normalize_index_url(stored, credential=True)


@pytest.mark.parametrize("url", [None, 5, 1.5, b"https://pkgs.example.org/simple", ["x"], object()])
def test_a_url_that_is_not_text_is_a_validation_error_never_a_type_error(url: object) -> None:
    """Both rule functions, whatever a caller of the service passes."""
    for credential in (False, True):
        with pytest.raises(ValidationError, match="must be text"):
            normalize_index_url(url, credential=credential)  # ty: ignore[invalid-argument-type]
    assert comparable_index_url(url) is None  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("url", [None, 5, b"https://mirror.corp/simple", ["x"]])
def test_is_known_never_raises_for_a_non_string_even_with_an_operator_index(url: object) -> None:
    sources = KnownSources(operator_index="https://mirror.corp/simple")
    assert not sources.is_known(url)  # ty: ignore[invalid-argument-type]
