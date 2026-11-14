"""A download's log lines name the artifact escaped."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lakana_core.identifiers import NamespacePath, TableRef
from lakana_server.api.platform.artifacts import DownloadCut, download_body, download_label

_HOSTILE = "x\n2026-10-08 ERROR forged record\x1b[2J"


def test_the_label_of_a_download_is_one_escaped_line() -> None:
    ref = TableRef("dev", NamespacePath.from_parts(["models"]), _HOSTILE)
    label = download_label(ref, 3)
    assert label == "models.x\\n2026-10-08 ERROR forged record\\x1b[2J version 3"


async def _drain(label: str) -> None:
    """Read a download that is longer than its manifest says, to the end.

    Parameters
    ----------
    label
        What the download is called.
    """
    async for _ in download_body(iter([b"abcdef"]), 3, label=label):
        pass


def test_a_cut_download_logs_and_raises_with_no_raw_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ref = TableRef("dev", NamespacePath.from_parts(["models"]), _HOSTILE)
    label = download_label(ref, 1)
    with caplog.at_level(logging.WARNING), pytest.raises(DownloadCut) as cut:
        asyncio.run(_drain(label))
    [record] = caplog.records
    for text in (record.getMessage(), str(cut.value)):
        assert "\n" not in text and "\x1b" not in text
        assert "\\x1b[2J" in text


def test_the_blob_route_labels_its_download_with_download_label() -> None:
    """A route that logged the raw name again would pass every other test here."""
    from types import SimpleNamespace

    from lakana_server.api.platform.artifacts import get_blob

    class Slots:
        """A download-slot limiter that always has room."""

        def try_acquire(self) -> bool:
            """Take a slot.

            Returns
            -------
            bool
                Always `True`.
            """
            return True

        def release(self) -> None:
            """Give the slot back."""

    class Service:
        """A service whose file is 3 bytes long."""

        def open_blob(self, *args: object) -> SimpleNamespace:
            """Open the file.

            Parameters
            ----------
            *args
                Ignored.

            Returns
            -------
            SimpleNamespace
                Pieces and size.
            """
            return SimpleNamespace(chunks=iter([b"abc"]), size=3)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(download_slots=Slots())))
    response = asyncio.run(
        get_blob(
            request,  # ty: ignore[invalid-argument-type]
            "dev",
            "models",
            _HOSTILE,
            "3",
            "0" * 64,
            principal=None,  # ty: ignore[invalid-argument-type]
            service=Service(),  # ty: ignore[invalid-argument-type]
        )
    )
    label = response._label  # ty: ignore[missing-attribute]
    assert label == "models.x\\n2026-10-08 ERROR forged record\\x1b[2J version 3"
