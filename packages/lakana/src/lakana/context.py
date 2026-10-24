"""What a running asset knows about its own run: `ctx`, and the state the helpers share.

The harness creates one `AssetContext` per attempt and passes it as the function's `ctx`
parameter when the function declares one. `current()` is how `lakana.ingest` finds the task's
client, the connections the asset declared and the values to hide; it exists only while the
harness runs the asset, and until the harness has written what the run left behind (its result
record and the trace of a failure), so the values to hide are still known then.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING
from urllib.parse import quote, quote_plus

if TYPE_CHECKING:
    from lakana.client import Client

WatermarkValue = int | Decimal | dt.date | dt.datetime
"""A watermark's value: what `AssetContext.watermark` returns."""
WATERMARK_TAGS = ("int", "long", "decimal", "date", "timestamp", "timestamptz")
"""The tags of the worker's encoding, one per column type a watermark may have."""
MIN_SECRET_CHARS = 4
"""Shortest value `register_secret` masks: a shorter one would mask ordinary text."""


def decode_watermark(text: str) -> WatermarkValue:
    """Decode the worker's ``<tag>:<value>`` form of a watermark.

    Parameters
    ----------
    text
        The encoded watermark.

    Returns
    -------
    int, Decimal, date or datetime
        The value; a ``timestamptz`` is UTC-aware, a ``timestamp`` naive.

    Raises
    ------
    ValueError
        For an unknown tag or a malformed value; the message names neither.
    """
    tag, separator, raw = text.partition(":")
    if not separator or tag not in WATERMARK_TAGS:
        raise ValueError("not a watermark: expected <type>:<value>")
    try:
        if tag in ("int", "long"):
            return int(raw)
        if tag == "decimal":
            return Decimal(raw)
        if tag == "date":
            return dt.date.fromisoformat(raw)
        return dt.datetime.fromisoformat(raw)
    except (ValueError, InvalidOperation):
        raise ValueError(f"not a watermark: the value of a {tag} watermark is malformed") from None


@dataclass(frozen=True)
class AssetContext:
    """The context of one run of an asset, passed as `ctx`.

    Parameters
    ----------
    watermark_value
        The decoded watermark, or `None` when the table has none yet or the asset has no
        `watermark`.
    """

    watermark_value: WatermarkValue | None = None

    def watermark(self, default: WatermarkValue | None = None) -> WatermarkValue | None:
        """Return the largest value of the asset's `watermark` column that its table holds.

        Parameters
        ----------
        default
            What to return on a first run, such as ``datetime(2020, 1, 1, tzinfo=UTC)``.

        Returns
        -------
        int, Decimal, date, datetime or None
            The value; `default` when the table has none yet (or the asset has no
            `watermark`).
        """
        return default if self.watermark_value is None else self.watermark_value


@dataclass
class _TaskState:
    """What the helpers share during one attempt.

    Parameters
    ----------
    client
        The SDK client holding the task's token.
    environment
        The environment's name.
    asset
        The asset's table.
    declared
        The connection names the asset declares.
    connections
        Routing per declared connection from the task description (`kind`, and from the
        Docker runner `relay_port`, `root_path`, `grant`).
    secrets
        Values the harness masks in everything it writes.
    """

    client: Client
    environment: str
    asset: str
    declared: frozenset[str]
    connections: Mapping[str, Mapping[str, object]]
    secrets: set[str] = field(default_factory=set)


_state: _TaskState | None = None


def current() -> _TaskState:
    """Return the running attempt's state.

    Returns
    -------
    _TaskState
        The state the harness set.

    Raises
    ------
    RuntimeError
        Outside a running task.
    """
    if _state is None:
        raise RuntimeError(
            "lakana.ingest reads connections only inside a Lakana task (a run of the deployed "
            "asset)"
        )
    return _state


# The copy of `lakana_core.secrets.forms.secret_forms`: the SDK cannot import lakana_core, and
# `lakana-worker/tests/test_secret_forms_parity.py` keeps the two the same.
# jscpd:ignore-start
def secret_forms(value: str) -> set[str]:
    """List the forms text can carry a credential in.

    The same list as `lakana_core.secrets.forms.secret_forms`, which the worker hides in the log
    it captures (the SDK cannot import that package; a test compares the two).

    Parameters
    ----------
    value
        The credential.

    Returns
    -------
    set of str
        The value itself; as it appears inside a Python `repr` (either quote), a JSON string
        (with and without ``ensure_ascii``), a URL (`quote` with its default ``/`` kept and with
        nothing kept, `quote_plus`) and the `repr` of its UTF-8 bytes; and each JSON form again
        inside a `repr`, the `repr` of its bytes and a second JSON string (a JSON body printed
        as bytes, in a dict or encoded twice). Forms shorter than `MIN_SECRET_CHARS` are left
        out.

    Raises
    ------
    UnicodeEncodeError
        For a value that cannot be written as UTF-8 (a lone surrogate); no form of it could be
        found in a log.
    """
    ascii_json = json.dumps(value)[1:-1]
    text_json = json.dumps(value, ensure_ascii=False)[1:-1]
    found = {value, ascii_json, text_json, quote(value), quote(value, safe=""), quote_plus(value)}
    for text in (value, ascii_json, text_json):
        # Inside a repr, with either quote: a repr picks `"` for text that holds a `'` alone.
        found |= {repr(text)[1:-1], repr(text + "\"'")[1:-4]}
        data = text.encode()
        found |= {repr(data)[2:-1], repr(data + b"\"'")[2:-4]}
    for text in (ascii_json, text_json):
        found |= {json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]}
    return {form for form in found if len(form) >= MIN_SECRET_CHARS}
    # jscpd:ignore-end


def register_secret(value: str) -> None:
    """Have the harness mask a value, in each of its `secret_forms`, in every text it writes.

    Parameters
    ----------
    value
        A credential; values shorter than `MIN_SECRET_CHARS` are ignored (masking them would
        mask ordinary text), and so is every value outside a running task.
    """
    if _state is not None and len(value) >= MIN_SECRET_CHARS:
        _state.secrets.update(secret_forms(value))


def _set(state: _TaskState | None) -> None:
    """Set or clear the state (the harness only).

    Parameters
    ----------
    state
        The state, or `None` once the attempt has written its record.
    """
    global _state
    _state = state
