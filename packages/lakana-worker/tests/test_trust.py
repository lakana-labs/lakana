"""`lakana_worker.trust`: the bundle every build and task container trusts."""

from __future__ import annotations

import os
import threading
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
    write_trust_file,
)

_FAKE = b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n"
_KEY = b"-----BEGIN PRIVATE KEY-----\nAA==\n"
_PGP = b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nAA==\n-----END PGP PRIVATE KEY BLOCK-----\n"


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


def test_no_operator_file_means_no_bundle() -> None:
    """Without LAKANA_RUN__CA_BUNDLE nothing is built: containers keep the image's trust."""
    assert build_trust_bundle(None) is None


def test_the_bundle_is_mozillas_roots_plus_the_operators_cas(
    tmp_path: Path, private_ca_pem: str
) -> None:
    """Public endpoints keep working; the private CA is added, never substituted."""
    operator = tmp_path / "company-ca.pem"
    operator.write_text(private_ca_pem)
    bundle = build_trust_bundle(operator)
    assert bundle is not None
    text = bundle.decode()
    assert text.startswith(Path(certifi.where()).read_text().rstrip())
    assert text.rstrip().endswith(private_ca_pem.rstrip())


def test_only_the_certificates_of_the_operator_file_reach_the_bundle(
    tmp_path: Path, private_ca_pem: str
) -> None:
    """Anything next to a certificate (a pasted secret, a DER blob) is never copied."""
    operator = tmp_path / "ca.pem"
    operator.write_bytes(
        b"my password is hunter2\n" + private_ca_pem.encode() + b"api_key=SECRETLINE\n\x00\x01"
    )
    bundle = build_trust_bundle(operator)
    assert bundle is not None
    assert b"hunter2" not in bundle and b"SECRETLINE" not in bundle and b"\x00" not in bundle
    assert bundle.rstrip().endswith(private_ca_pem.encode().rstrip())


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (b"not a certificate", "holds no PEM certificate"),
        (_FAKE, "is not a valid certificate"),
        (b"PGPDATA", "holds no PEM certificate"),
        (_KEY, "also holds a private key"),
        (_PGP, "also holds a private key"),
        (b"A" * (1024 * 1024 + 1), "is larger than 1 MiB"),
    ],
)
def test_a_bad_operator_file_stops_the_worker(tmp_path: Path, content: bytes, cause: str) -> None:
    """The message names the setting, the file and the cause; never the content."""
    operator = tmp_path / "ca.pem"
    operator.write_bytes(content)
    with pytest.raises(TrustBundleError, match=cause) as caught:
        build_trust_bundle(operator)
    assert "LAKANA_RUN__CA_BUNDLE" in str(caught.value)
    assert str(operator) in str(caught.value)
    assert "AA==" not in str(caught.value)


@pytest.mark.parametrize("key", [_KEY, _PGP], ids=["pkcs8", "pgp"])
def test_a_key_next_to_a_certificate_is_still_refused(
    tmp_path: Path, private_ca_pem: str, key: bytes
) -> None:
    """The operator learns of the key instead of having it dropped silently."""
    operator = tmp_path / "ca.pem"
    operator.write_bytes(private_ca_pem.encode() + key)
    with pytest.raises(TrustBundleError, match="also holds a private key"):
        build_trust_bundle(operator)


def test_a_trusted_certificate_block_is_refused_naming_the_conversion(
    tmp_path: Path, private_ca_pem: str
) -> None:
    """OpenSSL's ``-trustout`` form (RHEL's ca-bundle.trust.crt) is named, not dropped silently."""
    body = private_ca_pem.replace("BEGIN CERTIFICATE", "BEGIN TRUSTED CERTIFICATE").replace(
        "END CERTIFICATE", "END TRUSTED CERTIFICATE"
    )
    operator = tmp_path / "ca-bundle.trust.crt"
    operator.write_text(private_ca_pem + body)
    with pytest.raises(TrustBundleError) as caught:
        build_trust_bundle(operator)
    message = str(caught.value)
    assert "TRUSTED CERTIFICATE" in message
    assert "openssl x509 -in" in message and "-out" in message
    assert "only the" in message and "first certificate" in message
    assert "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem" in message
    assert str(operator) in message
    assert "holds no PEM certificate" not in message


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_an_operator_path_that_is_not_a_readable_file_stops_the_worker(
    tmp_path: Path, kind: str
) -> None:
    """A missing file or a directory is named with the cause."""
    operator = tmp_path / "ca.pem"
    if kind == "directory":
        operator.mkdir()
    with pytest.raises(TrustBundleError, match=r"LAKANA_RUN__CA_BUNDLE names .*ca\.pem, which"):
        build_trust_bundle(operator)


def test_unreadable_mozilla_roots_stop_the_worker_with_the_path(
    tmp_path: Path, private_ca_pem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing certifi file is a named cause, not a traceback."""
    operator = tmp_path / "ca.pem"
    operator.write_text(private_ca_pem)
    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "gone.pem"))
    with pytest.raises(TrustBundleError, match=r"gone\.pem.*No such file"):
        build_trust_bundle(operator)


def test_the_worker_file_is_named_by_its_content_and_written_atomically(tmp_path: Path) -> None:
    """A sibling worker with the same bundle writes the same file; another one, another file."""
    first = write_trust_file(b"one", tmp_path)
    assert first.read_bytes() == b"one" and oct(first.stat().st_mode & 0o777) == "0o644"
    assert write_trust_file(b"one", tmp_path) == first
    other = write_trust_file(b"two", tmp_path)
    assert other != first and first.read_bytes() == b"one"
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".pem", ".pem"]  # no temp left


def test_writing_the_worker_file_into_an_unwritable_place_names_the_path(tmp_path: Path) -> None:
    """The cause is named: path and strerror."""
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    with pytest.raises(TrustBundleError, match=r"cannot write the trust bundle to .*file"):
        write_trust_file(b"one", blocker / "cache")


def test_concurrent_workers_never_fail_and_a_deleted_file_changes_nothing(tmp_path: Path) -> None:
    """Replicas starting together share a directory; attempts use the bytes, not the file."""
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def worker(n: int) -> None:
        """Write the shared file and install into an attempt of this worker.

        Parameters
        ----------
        n
            The worker's number.
        """
        try:
            start.wait()
            for _ in range(20):
                path = write_trust_file(_FAKE, tmp_path / "cache")
                path.unlink(missing_ok=True)  # a sibling's cleanup or restart
                attempt = tmp_path / f"attempt-{n}"
                attempt.mkdir(mode=0o700, exist_ok=True)
                install_trust(_FAKE, attempt)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert (tmp_path / "attempt-3" / "trust" / "trust.pem").read_bytes() == _FAKE


def test_install_copies_the_bundle_into_the_attempt(tmp_path: Path) -> None:
    """A real file (not a link) in a 0755 directory, readable by the container's user."""
    bundle = _FAKE
    attempt = tmp_path / "attempt-a"
    attempt.mkdir(mode=0o700)
    install_trust(bundle, attempt)
    install_trust(bundle, attempt)  # the run after the build of the same attempt
    copied = attempt / "trust" / "trust.pem"
    assert not copied.is_symlink() and copied.read_bytes() == _FAKE
    assert oct(copied.stat().st_mode & 0o777) == "0o644"
    assert oct((attempt / "trust").stat().st_mode & 0o777) == "0o755"
    assert sorted(p.name for p in (attempt / "trust").iterdir()) == ["trust.pem"]


@pytest.mark.parametrize("planted", ["link", "file"])
def test_install_refuses_a_trust_path_that_is_not_the_workers_directory(
    tmp_path: Path, planted: str
) -> None:
    """A link or a file at `<attempt>/trust` is refused, never followed or overwritten."""
    bundle = _FAKE
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
    bundle = _FAKE
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
