"""`secret_forms`: every form a credential can take in text a task writes."""

from __future__ import annotations

import json

import pytest

from lakana_core.secrets.forms import MIN_SECRET_CHARS, secret_forms

_TRICKY = "Tr0ub\\4dor\"&'x\x01é/ +"


def test_every_form_a_print_a_repr_json_a_url_or_bytes_produce_is_listed() -> None:
    forms = secret_forms(_TRICKY)
    assert _TRICKY in forms
    assert "Tr0ub\\\\4dor\"&\\'x\\x01é/ +" in forms  # repr
    assert "Tr0ub\\\\4dor\\\"&'x\\u0001\\u00e9/ +" in forms  # JSON, ensure_ascii
    assert "Tr0ub\\\\4dor\\\"&'x\\u0001é/ +" in forms  # JSON, ensure_ascii=False
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9/%20%2B" in forms  # quote
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9%2F%20%2B" in forms  # quote, safe=""
    assert "Tr0ub%5C4dor%22%26%27x%01%C3%A9%2F+%2B" in forms  # quote_plus
    assert "Tr0ub\\\\4dor\"&\\'x\\x01\\xc3\\xa9/ +" in forms  # bytes repr


def test_a_plain_value_has_one_form() -> None:
    assert secret_forms("crm-db-pa55word") == {"crm-db-pa55word"}


def test_forms_shorter_than_the_minimum_are_left_out() -> None:
    assert MIN_SECRET_CHARS == 4
    assert secret_forms("abc") == set()
    short = secret_forms("a\nb")
    assert "a\nb" not in short
    assert {"a\\nb", "a%0Ab"} <= short
    assert all(len(form) >= MIN_SECRET_CHARS for form in short)


@pytest.mark.parametrize("value", ["Zürich-Pass-2026", 'Zq-quote"Pass'])
def test_a_json_form_printed_again_is_listed(value: str) -> None:
    """A JSON body printed as bytes, in a repr, or encoded as JSON a second time."""
    forms = secret_forms(value)
    for ensure_ascii in (True, False):
        body = json.dumps({"password": value}, ensure_ascii=ensure_ascii)
        for text in (
            str(body.encode()),
            repr({"body": body}),
            json.dumps(body),
            json.dumps(body, ensure_ascii=False),
        ):
            assert any(form in text for form in forms), text


def test_a_value_that_is_not_text_has_no_forms() -> None:
    with pytest.raises(UnicodeEncodeError):
        secret_forms("x\ud800yz")


def test_a_value_with_an_apostrophe_inside_a_printed_json_body_is_listed() -> None:
    """The body holds `"`, so its repr quotes with `'` and escapes the value's apostrophe."""
    value = "it's-a-pass"
    body = json.dumps({"password": value})
    assert any(form in repr({"body": body}) for form in secret_forms(value))
