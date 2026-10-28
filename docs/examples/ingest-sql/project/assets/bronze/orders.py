"""Orders from a source database, merged on their id as new ones arrive."""

from lakana import asset
from lakana.ingest import sql

# How many ids behind the largest one loaded each run reads again. A row gets its id when it is
# written but is seen only once its transaction commits, so a row with a smaller id can appear
# after a run read larger ones; without reading again it would be skipped for good. The merge on
# `id` keeps one row per id. Make it larger than the ids the source hands out while one of its
# transactions is open.
LOOK_BACK = 1000


@asset(connections=["source-db"], write="merge", key=["id"], watermark="id")
def orders(ctx):
    """Orders from the source database: each run reads from LOOK_BACK ids before the last one."""
    # For rows that change after they were written, use a change column as the watermark:
    # watermark="updated_at" with "... WHERE updated_at > :since", a datetime default, and the
    # look-back as a timedelta.
    return sql.read(
        "source-db",
        "SELECT * FROM orders WHERE id > :since ORDER BY id",
        params={"since": ctx.watermark(default=0) - LOOK_BACK},
    )
