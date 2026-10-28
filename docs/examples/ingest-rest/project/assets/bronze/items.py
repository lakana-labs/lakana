"""Items from a source REST API, merged on their id as they change."""

from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.compute as pc

from lakana import asset
from lakana.ingest import rest


@asset(connections=["source-api"], write="merge", key=["id"], watermark="updated_at")
def items(ctx):
    """Items changed since the last run, merged into the table on their id."""
    changed = rest.read(
        "source-api",
        "items",
        params={"updated_since": ctx.watermark(default=datetime(2000, 1, 1, tzinfo=UTC))},
        paginate=rest.NextLink(),
        records="value",
    )
    if "updated_at" not in changed.column_names:
        return changed  # nothing changed since the last run: the run writes nothing
    # JSON has no timestamp type: the API's ISO 8601 text becomes one, so it can be the watermark.
    at = changed.schema.get_field_index("updated_at")
    return changed.set_column(
        at, "updated_at", pc.cast(changed["updated_at"], pa.timestamp("us", "UTC"))
    )
