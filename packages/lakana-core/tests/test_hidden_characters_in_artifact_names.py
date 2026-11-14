"""An artifact is listed among a namespace's tables, so its new name meets the rule of #593."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from core_test_helpers import ARTIFACT_REF, artifact_document, artifact_service
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.models import Table

_FILES = {"m": b"weights"}


@pytest.mark.parametrize(("character", "code"), [("​", "U+200B"), ("‮", "U+202E")])
def test_a_new_artifact_name_with_a_hidden_character_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef, character: str, code: str
) -> None:
    service = artifact_service(session, tmp_path)
    twin = dataclasses.replace(ARTIFACT_REF, name=f"churn{character}")

    with pytest.raises(ValidationError, match=rf"Artifact name .*{re.escape(code)}"):
        service.begin_upload(principal, twin, artifact_document(_FILES))
    assert session.query(Table).filter(Table.name == twin.name).count() == 0


def test_a_stored_artifact_name_with_a_hidden_character_keeps_working(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    session.commit()
    stored = dataclasses.replace(ARTIFACT_REF, name="churn​")
    session.execute(update(Table).where(Table.name == "churn").values(name=stored.name))
    session.commit()

    assert service.begin_upload(principal, stored, artifact_document(_FILES)).upload_id
