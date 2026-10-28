"""Rows of the CSV files a source system exports into a directory."""

from lakana import asset
from lakana.ingest import files


@asset(connections=["exports"], write="append", watermark="_source_modified")
def exports(ctx):
    """Rows of the exported CSV files, appended as new files arrive."""
    # files.read adds `_source_modified`, the time each file was last changed, and with since=
    # reads only files changed after it. On the first run ctx.watermark() is None, which is
    # right here: every file is new, so every file is read.
    return files.read("exports", "*.csv", since=ctx.watermark())
