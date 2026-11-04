"""Read the `E2E <json>` report a Docker test's task prints.

The task's stdout and stderr share one stream, so another write can land on the report's line.
These helpers read the report anyway.
"""

from __future__ import annotations

import json
from typing import Any

_PREFIX = "E2E "


def last_report(text: str) -> Any | None:  # ruff: ignore[any-type]
    """Return the last `E2E <json>` report in a task's output.

    A report is the JSON value after `E2E `; output that follows it on the same line is
    ignored, and several reports on one line are all found. Text after the prefix that is not
    valid JSON is skipped.

    Parameters
    ----------
    text : str
        The task's whole output.

    Returns
    -------
    Any | None
        The last report decoded, or `None` when the output holds none.
    """
    decoder = json.JSONDecoder()
    found: Any | None = None
    position = text.find(_PREFIX)
    while position != -1:
        start = position + len(_PREFIX)
        try:
            found, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            position = text.find(_PREFIX, start)
        else:
            position = text.find(_PREFIX, end)
    return found
