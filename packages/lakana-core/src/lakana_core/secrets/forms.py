"""The forms a credential takes in text a task writes, so that each form can be hidden.

The worker hides these in the log it captures from a task; the task's harness hides the same
forms in the texts it writes (`lakana.context.secret_forms`, which the SDK keeps as its own
copy because it cannot import this package; a test compares the two).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import quote, quote_plus

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown

MIN_SECRET_CHARS = 4
"""Shortest form that is hidden: a shorter one would hide ordinary text."""


def secret_forms(value: str) -> set[str]:
    """List the forms text can carry a credential in.

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


def require_text(values: Mapping[str, str]) -> None:
    r"""Refuse a secret value that cannot be written as UTF-8 text, naming only its key.

    JSON lets a body carry a lone surrogate (``"\ud800"``). Such a value cannot be hidden in a
    task's log (`lakana_core.secrets.forms.secret_forms` cannot write it), so it is never
    stored.

    Parameters
    ----------
    values
        The plaintext values.

    Raises
    ------
    ValidationError
        For the first key whose value is not valid text; the message never holds the value.
    """
    for key, value in values.items():
        try:
            value.encode()
        except UnicodeEncodeError:
            raise ValidationError(
                f"the value of `{shown(key)}` is not valid text (it cannot be written as "
                "UTF-8); give it again as plain text"
            ) from None
