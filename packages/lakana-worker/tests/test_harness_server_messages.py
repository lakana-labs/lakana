"""The task harness and the server agree on the schema-mismatch message.

The harness (`lakana.runner`, in the SDK) rewords the server's refusal of a
changed output schema: it keeps the server's description of what differs and
cuts the server's advice to pass ``evolve_schema`` (or ``--evolve-schema``), which a
Python asset gives as ``@asset(evolve_schema=True)`` instead. It finds that advice by its
opening words. The SDK must not import core, so this test lives here: if the server's
message is reworded, it fails without needing a real run.

A SQL asset's message is reworded by the worker the same way (`sql_asset_mismatch_message`).

The SDK's artifact client compares the server's answers with three more texts, and this file
pins two of them: `_UPLOAD_GONE` and `_NOT_STORED`. The third, `_BUSY_TYPE`, names a server
class, which this package may not import; `test_artifacts_read.py` of `lakana-server` pins it.
"""

from __future__ import annotations

from lakana import runner
from lakana.errors import ValidationError
from lakana.resources import artifacts
from lakana_core.artifacts.service import FILES_NOT_STORED, UPLOAD_GONE
from lakana_core.catalog.merge import COMPOSITE_KEY_MAX_ROWS
from lakana_core.catalog.schema_compat import (
    EVOLVE_ADVICE,
    ColumnType,
    SchemaDiff,
    SchemaMismatchError,
)
from lakana_core.models import WriteMode
from lakana_worker.task_outcome import sql_asset_mismatch_message


def test_the_harness_cuts_exactly_the_advice_the_server_really_gives() -> None:
    diff = SchemaDiff(dropped=(ColumnType("region", "string"),))
    server = SchemaMismatchError("gold.scores", diff)
    text = str(server)
    assert text.count(runner._SERVER_ADVICE) == 1
    assert text.split(runner._SERVER_ADVICE)[0].endswith(f"({diff.describe()}).")
    refusal = ValidationError(text, status=422, error_type=type(server).__name__)
    assert runner._upload_message(refusal, "gold.scores", "replace") == (
        "the asset's output no longer matches the schema of gold.scores. Server detail: The "
        f"result does not match gold.scores's schema ({diff.describe()}). To let gold.scores "
        "take the new schema, add evolve_schema=True to the asset's @asset(...) and deploy "
        "again (a column the output no longer has is then removed from the table); otherwise "
        "restore the columns and types the table has."
    )


def test_the_sdk_recognises_the_servers_expired_upload_text() -> None:
    assert artifacts._UPLOAD_GONE == UPLOAD_GONE


def test_the_sdk_recognises_the_servers_files_not_stored_text() -> None:
    text = FILES_NOT_STORED.format(count=3)
    assert artifacts._NOT_STORED in text
    assert text == "3 of this upload's files are not stored yet; upload them and commit again."


def test_the_harness_refuses_a_merge_over_the_servers_own_cap() -> None:
    """The harness checks a merge on several key columns before it sends it, at the same cap."""
    assert runner.COMPOSITE_KEY_MAX_ROWS == COMPOSITE_KEY_MAX_ROWS


def test_the_worker_rewords_the_advice_the_server_gives_and_keeps_any_other_text() -> None:
    """A SQL asset's message replaces exactly the server's last sentence (#377)."""
    text = str(SchemaMismatchError("gold.t", SchemaDiff(dropped=(ColumnType("a", "long"),))))
    assert text.endswith(EVOLVE_ADVICE)
    reworded = sql_asset_mismatch_message(text, "gold.t", WriteMode.REPLACE)
    assert EVOLVE_ADVICE not in reworded
    assert reworded.startswith("The result does not match gold.t's schema (dropped a (long)). A ")
    assert sql_asset_mismatch_message("other words", "gold.t", WriteMode.REPLACE) == "other words"
