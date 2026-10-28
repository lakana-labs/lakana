"""Items from a source REST API, merged on their id as they change."""

from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from lakana import asset
from lakana.ingest import rest

# How far behind the newest `updated_at` loaded each run asks again. The source sets updated_at
# when it writes an item, but the API shows the item only once that write commits, so an item
# can appear after a run read newer ones; without asking again it would be skipped for good.
# The merge on `id` keeps one row per item. Make it longer than the source's longest write.
LOOK_BACK = timedelta(hours=1)


@asset(connections=["source-api"], write="merge", key=["id"], watermark="updated_at")
def items(ctx):
    """Items changed since LOOK_BACK before the last run's newest, merged on their id."""
    since = ctx.watermark(default=datetime(2000, 1, 1, tzinfo=UTC)) - LOOK_BACK
    changed = rest.read(
        "source-api",
        "items",
        params={"updated_since": since},
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
