"""Orders from a source database, appended as new ones arrive."""

from lakana import asset
from lakana.ingest import sql


@asset(connections=["source-db"], write="append", watermark="id")
def orders(ctx):
    """New orders from the source database: each run reads the ids after the last one loaded."""
    # For rows that change after they were written, merge on the key instead:
    # @asset(connections=["source-db"], write="merge", key=["id"], watermark="updated_at")
    # with "... WHERE updated_at > :since" and a datetime default.
    return sql.read(
        "source-db",
        "SELECT * FROM orders WHERE id > :since ORDER BY id",
        params={"since": ctx.watermark(default=0)},
    )
