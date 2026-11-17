"""A grant made twice through the SDK, against the real app (#673)."""

from __future__ import annotations

from pathlib import Path

import pytest

import lakana


def test_the_sdk_raises_conflict_for_an_identical_grant(
    lakana_client: lakana.Client, tmp_path: Path
) -> None:
    """`client.grants.grant` raises `lakana.Conflict` with the same sentence."""
    lakana_client.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}", "options": {}}
    )
    lakana_client.groups.create("eng")
    first = lakana_client.grants.grant(group="eng", action="READ_TABLE", kind="INSTANCE")

    with pytest.raises(lakana.Conflict) as refused:
        lakana_client.grants.grant(group="eng", action="READ_TABLE", kind="INSTANCE")

    assert str(first.id) in str(refused.value)
    assert "Group 'eng' already holds READ_TABLE on the instance" in str(refused.value)
