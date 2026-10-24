"""The worker and the task's harness hide the same forms of a credential.

The worker cannot import the SDK (`lakana.context.secret_forms`), nor the SDK core
(`lakana_core.secrets.forms.secret_forms`); both lists must stay the same.
"""

from __future__ import annotations

import pytest

from lakana import context
from lakana_core.secrets import forms

_CORPUS = [
    "plain-password",
    "Tr0ub\\4dor\"&'x",
    "ctl\x01\x7f\tend",
    "naïve-é-ß-日本",
    "slash/plus+space q",
    "\U0001f511-emoji",
    "it's",
    'say "hi"',
    "abc",
    "",
    "Zürich-Pass-2026",
    'Zq-quote"Pass',
    "back\\slash\\and'quote\"",
]


@pytest.mark.parametrize("value", _CORPUS)
def test_the_worker_and_the_harness_list_the_same_forms(value: str) -> None:
    assert forms.secret_forms(value) == context.secret_forms(value)


def test_the_minimum_length_is_the_same() -> None:
    assert forms.MIN_SECRET_CHARS == context.MIN_SECRET_CHARS


def test_both_refuse_a_value_that_is_not_text() -> None:
    for secret_forms in (forms.secret_forms, context.secret_forms):
        with pytest.raises(UnicodeEncodeError):
            secret_forms("x\ud800yz")
