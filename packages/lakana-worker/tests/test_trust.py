"""`lakana_worker.trust`: the bundle every build and task container trusts."""

from __future__ import annotations

import os
from pathlib import Path

import certifi
import pytest
from tls_fixture import make_authority

from lakana_worker.trust import (
    CERTIFICATE_HINT,
    TrustBundleError,
    build_trust_bundle,
    certificate_hint,
    install_trust,
)

_CERTIFICATE = b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"


@pytest.fixture
def private_ca_pem(tmp_path: Path) -> str:
    """Make a private test authority and return its certificate, PEM.

    Parameters
    ----------
    tmp_path
        Where the authority's file is written.

    Returns
    -------
    str
        The certificate.
    """
    return make_authority(tmp_path).ca_file.read_text()


def test_no_operator_file_means_no_bundle(tmp_path: Path) -> None:
    """Without LAKANA_RUN__CA_BUNDLE nothing is written: containers keep the image's trust."""
    assert build_trust_bundle(None, tmp_path / "trust.pem") is None
    assert not (tmp_path / "trust.pem").exists()


def test_a_bundle_of_an_earlier_start_is_removed_without_an_operator_file(tmp_path: Path) -> None:
    """Unsetting the setting and restarting leaves no stale bundle behind."""
    (tmp_path / "trust.pem").write_bytes(_CERTIFICATE)
    assert build_trust_bundle(None, tmp_path / "trust.pem") is None
    assert not (tmp_path / "trust.pem").exists()


def test_the_bundle_is_mozillas_roots_plus_the_operators_cas(
    tmp_path: Path, private_ca_pem: str
) -> None:
    """Public endpoints keep working; the private CA is added, never substituted."""
    operator = tmp_path / "company-ca.pem"
    operator.write_text(private_ca_pem)
    bundle = build_trust_bundle(operator, tmp_path / "cache" / "trust.pem")
    assert bundle == tmp_path / "cache" / "trust.pem"
    text = bundle.read_text()
    assert text.startswith(Path(certifi.where()).read_text().rstrip())
    assert text.rstrip().endswith(private_ca_pem.rstrip())
    assert oct(bundle.stat().st_mode & 0o777) == "0o644"


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (b"not a certificate", "holds no PEM certificate"),
        (_CERTIFICATE + b"-----BEGIN PRIVATE KEY-----\nAA==\n", "also holds a private key"),
        (_CERTIFICATE + b"-----BEGIN EC PRIVATE KEY-----\nAA==\n", "also holds a private key"),
        (_CERTIFICATE + b"A" * (1024 * 1024), "is larger than 1 MiB"),
    ],
)
def test_a_bad_operator_file_stops_the_worker(tmp_path: Path, content: bytes, cause: str) -> None:
    """The message names the setting, the file and the cause; never the content."""
    operator = tmp_path / "ca.pem"
    operator.write_bytes(content)
    with pytest.raises(TrustBundleError, match=cause) as caught:
        build_trust_bundle(operator, tmp_path / "trust.pem")
    assert "LAKANA_RUN__CA_BUNDLE" in str(caught.value)
    assert str(operator) in str(caught.value)
    assert "AA==" not in str(caught.value)
    assert not (tmp_path / "trust.pem").exists()


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_an_operator_path_that_is_not_a_readable_file_stops_the_worker(
    tmp_path: Path, kind: str
) -> None:
    """A missing file or a directory is named with the cause, before anything is written."""
    operator = tmp_path / "ca.pem"
    if kind == "directory":
        operator.mkdir()
    with pytest.raises(TrustBundleError, match=r"LAKANA_RUN__CA_BUNDLE names .*ca\.pem, which"):
        build_trust_bundle(operator, tmp_path / "trust.pem")


def test_install_copies_the_bundle_into_the_attempt(tmp_path: Path) -> None:
    """A real file (not a link) in a 0755 directory, readable by the container's user."""
    bundle = tmp_path / "trust.pem"
    bundle.write_bytes(_CERTIFICATE)
    attempt = tmp_path / "attempt-a"
    attempt.mkdir(mode=0o700)
    install_trust(bundle, attempt)
    install_trust(bundle, attempt)  # the run after the build of the same attempt
    copied = attempt / "trust" / "trust.pem"
    assert not copied.is_symlink() and copied.read_bytes() == _CERTIFICATE
    assert oct(copied.stat().st_mode & 0o777) == "0o644"
    assert oct((attempt / "trust").stat().st_mode & 0o777) == "0o755"
    assert sorted(p.name for p in (attempt / "trust").iterdir()) == ["trust.pem"]


@pytest.mark.parametrize("planted", ["link", "file"])
def test_install_refuses_a_trust_path_that_is_not_the_workers_directory(
    tmp_path: Path, planted: str
) -> None:
    """A link or a file at `<attempt>/trust` is refused, never followed or overwritten."""
    bundle = tmp_path / "trust.pem"
    bundle.write_bytes(_CERTIFICATE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    attempt = tmp_path / "attempt-a"
    attempt.mkdir(mode=0o700)
    if planted == "link":
        os.symlink(elsewhere, attempt / "trust")
    else:
        (attempt / "trust").write_bytes(b"x")
    with pytest.raises(OSError, match="not a directory of the worker's own"):
        install_trust(bundle, attempt)
    assert list(elsewhere.iterdir()) == []


def test_install_replaces_a_link_planted_at_the_file(tmp_path: Path) -> None:
    """A link at `trust/trust.pem` is replaced by the bundle, its target left alone."""
    bundle = tmp_path / "trust.pem"
    bundle.write_bytes(_CERTIFICATE)
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    attempt = tmp_path / "attempt-a"
    (attempt / "trust").mkdir(parents=True, mode=0o755)
    os.symlink(target, attempt / "trust" / "trust.pem")
    install_trust(bundle, attempt)
    assert not (attempt / "trust" / "trust.pem").is_symlink()
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "output",
    [
        "error: invalid peer certificate: UnknownIssuer",
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer",
    ],
)
def test_an_untrusted_certificate_in_a_build_names_the_setting(output: str) -> None:
    """Both uv's and Python's wording give the hint; other failures do not."""
    assert certificate_hint(output) == CERTIFICATE_HINT
    assert "LAKANA_RUN__CA_BUNDLE" in CERTIFICATE_HINT
    assert certificate_hint("error: No solution found") is None
