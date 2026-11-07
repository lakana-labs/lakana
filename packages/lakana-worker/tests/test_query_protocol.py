"""Tests for the request and error documents of the query child."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest

from lakana_worker.query_protocol import (
    MAX_ERROR_BYTES,
    MAX_ERROR_LINE_BYTES,
    MAX_REQUEST_BYTES,
    WRITE,
    ErrorDocument,
    Request,
    decode_error,
    decode_request,
    encode_error,
    encode_request,
)


def _request(**overrides: object) -> Request:
    """Build a request with every kind of bound value.

    Parameters
    ----------
    overrides
        Fields to replace.

    Returns
    -------
    Request
        The request.
    """
    fields: dict[str, Any] = {
        "mode": WRITE,
        "sql": "SELECT * FROM read_parquet($u) WHERE k > $w",
        "parameters": {
            "u": ["https://store.example/a.parquet?X-Amz-Signature=abc"],
            "i": 2**40,
            "d": Decimal("12.340"),
            "day": dt.date(2026, 10, 8),
            "naive": dt.datetime(2026, 10, 8, 12, 30, 1, 500),
            "aware": dt.datetime(2026, 10, 8, 12, 30, tzinfo=dt.UTC),
        },
        "read_prefixes": ("https://store.example/",),
        "ca_bundle": "/etc/ssl/certs/ca-certificates.crt",
        "spill": False,
        "memory_limit": "1GB",
        "threads": 4,
        "temp_directory": "/tmp/q/tmp",
        "max_temp_directory_size": "10GB",
        "batch_rows": 65536,
        "extension_directory": "/home/lakana/.duckdb/extensions",
        "parent_pid": 4711,
    }
    fields.update(overrides)
    return Request(**fields)


def test_a_request_survives_the_round_trip_with_every_type_intact() -> None:
    request = _request()
    decoded = decode_request(encode_request(request))
    assert decoded == request
    assert [type(v) for v in decoded.parameters.values()] == [
        list,
        int,
        Decimal,
        dt.date,
        dt.datetime,
        dt.datetime,
    ]
    aware, naive = decoded.parameters["aware"], decoded.parameters["naive"]
    assert isinstance(aware, dt.datetime) and aware.tzinfo is not None
    assert isinstance(naive, dt.datetime) and naive.tzinfo is None


def test_the_document_has_exactly_the_fields_the_child_is_given() -> None:
    """The whole of what crosses to the child; a new field must be argued for here."""
    assert set(json.loads(encode_request(_request()))) == {
        "mode",
        "sql",
        "parameters",
        "read_prefixes",
        "ca_bundle",
        "spill",
        "memory_limit",
        "threads",
        "temp_directory",
        "max_temp_directory_size",
        "batch_rows",
        "extension_directory",
        "parent_pid",
        "extension_source",
    }


@pytest.mark.parametrize("value", [True, 1.5, "text", None, b"bytes", ["a", 1], {"a": 1}])
def test_a_value_the_platform_does_not_bind_is_refused_by_name_not_by_value(value: object) -> None:
    with pytest.raises(TypeError) as caught:
        encode_request(_request(parameters={"__lakana_p": value}))
    assert str(caught.value).startswith(
        "Query parameter '__lakana_p' has a type the query process cannot take: "
    )
    assert "text" not in str(caught.value) or value != "text"


def _document(**changes: object) -> bytes:
    """Encode a request, then change or remove fields of the JSON document.

    Parameters
    ----------
    changes
        Fields to set; `...` removes the field.

    Returns
    -------
    bytes
        The changed JSON document.
    """
    document = json.loads(encode_request(_request()))
    for key, value in changes.items():
        if value is ...:
            del document[key]
        else:
            document[key] = value
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff\xfe", "query request: not a JSON document"),
        (b"{", "query request: not a JSON document"),
        (b"[1]", "query request: not a JSON object"),
        (_document(surprise=1), "query request: unknown field 'surprise'"),
        (_document(sql=...), "query request: sql must be text"),
        (_document(memory_limit=5), "query request: memory_limit must be text"),
        (_document(extension_directory=None), "query request: extension_directory must be text"),
        (_document(mode="drop"), "query request: mode must be read, write, describe or check"),
        (_document(spill="yes"), "query request: spill must be true or false"),
        (_document(spill=...), "query request: spill must be true or false"),
        (_document(ca_bundle=None), "query request: ca_bundle must be text"),
        (_document(read_prefixes="/a/"), "query request: read_prefixes must be a list of texts"),
        (
            _document(read_prefixes=["/a/", 1]),
            "query request: read_prefixes must be a list of texts",
        ),
        (_document(read_prefixes=...), "query request: read_prefixes must be a list of texts"),
        (_document(parameters=[]), "query request: parameters must be an object"),
        (_document(threads=0), "query request: threads must be a positive integer"),
        (_document(batch_rows=True), "query request: batch_rows must be a positive integer"),
        (_document(parent_pid="1"), "query request: parent_pid must be a positive integer"),
        (_document(parameters={"p": 5}), "query request: parameter 'p' is not one tagged value"),
        (
            _document(parameters={"p": {"int": "1", "date": "x"}}),
            "query request: parameter 'p' is not one tagged value",
        ),
        (
            _document(parameters={"p": {"strings": ["a", 1]}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"int": 5}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"int": "five"}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"decimal": "1,5"}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"date": "yesterday"}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"timestamp": "noon"}}),
            "query request: parameter 'p' has an unreadable value",
        ),
        (
            _document(parameters={"p": {"float": "1.5"}}),
            "query request: parameter 'p' has an unknown type",
        ),
    ],
)
def test_a_document_that_is_not_a_request_is_refused_with_the_field(
    raw: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match="query request") as caught:
        decode_request(raw)
    assert str(caught.value) == message


def test_an_error_document_is_one_line_and_survives_the_round_trip() -> None:
    error = ErrorDocument("out_of_memory", "Query failed: first line\nsecond line")
    line = encode_error(error)
    assert "\n" not in line
    assert decode_error(line.encode() + b"\n") == error


@pytest.mark.parametrize("unit", ["x", "\u00e4", "\x01", "\U0001f600", '"'])
def test_a_long_message_is_cut_in_bytes_on_a_character_boundary_on_both_sides(unit: str) -> None:
    """Whatever the characters, the encoded line stays under 32 KiB and decodes."""
    line = encode_error(ErrorDocument("invalid", unit * 20_000))
    assert len(line.encode()) <= MAX_ERROR_LINE_BYTES < 32 * 1024
    decoded = decode_error(line.encode())
    assert decoded is not None
    assert len(decoded.message.encode()) <= MAX_ERROR_BYTES
    assert decoded.message.endswith("\u2026") and "\ufffd" not in decoded.message
    count = MAX_ERROR_BYTES // len(unit.encode()) + 400
    forged = json.dumps(
        {"lakana_query_error": 1, "kind": "invalid", "message": unit * count}, ensure_ascii=False
    ).encode()
    again = decode_error(forged)
    if len(forged) <= MAX_ERROR_LINE_BYTES:
        assert again is not None and len(again.message.encode()) <= MAX_ERROR_BYTES
        assert "\ufffd" not in again.message
    else:
        assert again is None


def test_a_message_within_the_bound_is_not_touched() -> None:
    error = ErrorDocument("invalid", "\u00e4" * (MAX_ERROR_BYTES // 2))
    assert decode_error(encode_error(error).encode()) == error


def test_the_document_is_the_last_line_behind_whatever_else_was_printed() -> None:
    line = encode_error(ErrorDocument("transient", "Query failed: IO Error"))
    noisy = b"warning from an extension\n\xff\xfe broken bytes\n" + line.encode() + b"\n\n"
    assert decode_error(noisy) == ErrorDocument("transient", "Query failed: IO Error")


@pytest.mark.parametrize(
    "stderr",
    [
        b"",
        b"\n\n",
        b"Traceback (most recent call last):\n  ...\nRuntimeError: boom\n",
        b'{"kind": "invalid", "message": "no mark"}\n',
        b'["lakana_query_error"]\n',
        b'{"lakana_query_error": 1, "kind": "made_up", "message": "x"}\n',
        b'{"lakana_query_error": 1, "kind": "invalid", "message": 5}\n',
        b'{"lakana_query_error": 1, "kind": "invalid", "message": "x"}\nand a line after it\n',
    ],
)
def test_anything_else_on_standard_error_is_not_an_error_document(stderr: bytes) -> None:
    assert decode_error(stderr) is None


@pytest.mark.parametrize(
    "stderr",
    [
        b'{"lakana_query_error": 1, "kind": [], "message": "x"}\n',
        b'{"lakana_query_error": 1, "kind": {}, "message": "x"}\n',
        b'{"lakana_query_error": 1, "kind": 5, "message": "x"}\n',
        b'{"lakana_query_error": 1, "kind": "invalid", "message": null}\n',
        b'{"lakana_query_error": [], "kind": "invalid", "message": "x"}\n',
        b"[" * 60_000 + b"\n",
        b"[" * 20_000 + b"]" * 20_000 + b"\n",
        b'{"lakana_query_error": 1, "kind": "invalid", "message": "\xff\xfe"}\n',
        b"\xff\xfe\xfd",
        b'{"lakana_query_error": 1, "kind": "invalid", "message": "' + b"y" * 200_000 + b'"}\n',
        b"x" * 500_000,
    ],
)
def test_decode_error_answers_none_for_any_last_line_that_is_not_a_document(
    stderr: bytes,
) -> None:
    assert decode_error(stderr) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_document(mode=["read"]), "query request: mode must be read, write, describe or check"),
        (_document(mode={}), "query request: mode must be read, write, describe or check"),
        (b"[" * 100_000, "query request: not a JSON document"),
        (b'{"a":' * 100_000, "query request: not a JSON document"),
        (b" " * (MAX_REQUEST_BYTES + 1), "query request: larger than 64 MiB"),
        (
            _document(**{"x" * 5000: 1}),
            "query request: unknown field '" + "x" * 59,
        ),
        (
            _document(parameters={"p" * 5000: 5}),
            "query request: parameter '" + "p" * 59,
        ),
    ],
)
def test_decode_request_refuses_every_shape_with_its_own_error_and_never_a_traceback(
    raw: bytes, message: str
) -> None:
    with pytest.raises(ValueError, match="query request") as caught:
        decode_request(raw)
    assert str(caught.value).startswith(message)
    assert len(str(caught.value)) < 200


def test_a_field_id_map_survives_the_round_trip() -> None:
    fields = ((1, "id", "BIGINT"), (2, "full name", 'STRUCT("a" INTEGER)'))
    request = _request(parameters={"v_0_schema": fields})

    decoded = decode_request(encode_request(request))

    assert decoded.parameters == {"v_0_schema": fields}


@pytest.mark.parametrize(
    "raw",
    [[[1, "id"]], [[True, "id", "BIGINT"]], [["1", "id", "BIGINT"]], [[1, "id", 5]]],
)
def test_a_malformed_field_id_map_is_refused(raw: object) -> None:
    with pytest.raises(ValueError, match="parameter 'p' has an unreadable value"):
        decode_request(_document(parameters={"p": {"field_ids": raw}}))


def test_the_extension_source_survives_the_round_trip_and_defaults_to_duckdb() -> None:
    assert decode_request(encode_request(_request())).extension_source == "duckdb"
    assert decode_request(encode_request(_request(extension_source="none"))).extension_source == (
        "none"
    )


@pytest.mark.parametrize(
    "value",
    ["", "https://mirror.example/x", "http://m.example", "NONE", "off", "none ", 5, None],
)
def test_the_child_refuses_an_extension_source_that_is_not_duckdb_or_none(value: object) -> None:
    document = json.loads(encode_request(_request()))
    document["extension_source"] = value
    with pytest.raises(ValueError, match="extension_source must be duckdb or none"):
        decode_request(json.dumps(document).encode())
