"""Tests of the backup and restore drill, `docs/examples/compose-restore.sh` (#389).

The drill runs only in CI's ``compose-smoke`` job (never ``docker compose up`` locally), so these
tests pin what it must prove after the restore: the snapshot, the grants, the secrets, and a table
of another environment whose storage secret has to decrypt. The page must state the restore order,
the RPO and the RTO.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DRILL = ROOT / "docs" / "examples" / "compose-restore.sh"
PAGE = ROOT / "docs" / "site" / "how-to" / "deploy-with-compose.md"


def _drill() -> str:
    """Read the drill.

    Returns
    -------
    str
        Its text.
    """
    return DRILL.read_text()


def _after_restore() -> str:
    """Return the part of the drill after the restore starts.

    Returns
    -------
    str
        The text after the ``restore-directory`` region begins.
    """
    return _drill().split("# docs:start restore-directory", 1)[1]


def test_the_drill_is_valid_bash() -> None:
    """A syntax error would only show in the CI job."""
    subprocess.run(["bash", "-n", str(DRILL)], check=True, capture_output=True)


def test_the_drill_records_the_state_before_the_backup() -> None:
    """The snapshot, grants and secrets are noted before the backup, to compare after."""
    before = _drill().split("# docs:start backup", 1)[0]
    assert "table get bronze.demo" in before
    assert "grant list" in before
    assert "secret list" in before
    assert "secret create" in before
    assert "grant create" in before


def test_the_drill_compares_snapshot_grants_and_secrets_after_the_restore() -> None:
    """The restored instance has the same snapshot id, grants and secrets."""
    after = _after_restore()
    assert '"$(snapshot)" = "$snapshot_before"' in after
    assert '"$grants_after" = "$grants_before"' in after
    assert '"$(secrets)" = "$secrets_before"' in after


def test_the_drill_reads_a_table_whose_storage_secret_must_decrypt() -> None:
    """A table of the second environment reads back: the master key still decrypts its secret."""
    assert "--env prod" in _after_restore()


def test_the_drill_reports_how_long_the_restore_took() -> None:
    """The time of the restore (RTO) is printed, so a CI log shows it."""
    assert "SECONDS" in _drill()
    assert "restore took" in _drill()


def test_the_page_states_the_restore_order_the_rpo_and_the_rto() -> None:
    """The restore section names RPO and RTO and the order: catalog first, then storage."""
    page = PAGE.read_text()
    restore = page.split("## Restore", 1)[1].split("## After a restore", 1)[0]
    assert "RPO" in restore
    assert "RTO" in restore
    assert restore.index("restore-catalog") < restore.index("restore-storage")
