"""A new namespace, table or column name is NFC and holds only letters, digits and a few symbols.

Two Unicode spellings of one name (NFC and NFD) compare unequal but look the same; a combining
grapheme joiner, a variation selector, a Hangul filler or the braille blank is drawn as nothing;
a no-break or other wide space looks like a plain one. So a new name, a column's included, must
be in Unicode NFC, hold none of those, and put a combining mark only after a letter. A new
namespace, table or view name may moreover hold only letters, decimal digits, ``_``, ``-`` and
the plain space; a column name may hold visible symbols, such as ``count(*)`` (#609). Only a
name being created is judged; a stored one keeps working.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table.update import AddSchemaUpdate
from pyiceberg.types import LongType, NestedField
from sqlalchemy import update
from sqlalchemy.orm import Session
from test_hidden_characters_in_names import _metadata_with_column
from test_table_location_sweep import _schema
from test_table_writes import _catalog, _ref, _write

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import apply_updates
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import (
    NAME_SYMBOLS,
    NamespacePath,
    TableRef,
    character_outside_name_allowlist,
    check_new_column_name,
    check_new_name,
    look_alike_character,
    refuse_characters_outside_name_allowlist,
    refuse_look_alike_characters,
)
from lakana_core.models import Namespace, Table, WriteMode

_NFD_CAFE = "cafe\u0301"
_NFC_CAFE = "caf\xe9"
_CGJ = "\u034f"


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` at `tmp_path` and return a catalog over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _catalog(session, principal, tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "orders",
        "Bestellungen_2026",
        "daily-orders",
        "daily orders 2026",
        _NFC_CAFE,
        "\u6ce8\u6587",
        "\u0939\u093f\u0928\u094d\u0926\u0940",  # Hindi: marks NFC cannot compose stay
        "q\u0303",  # q with tilde has no precomposed form, so it stays two characters in NFC
        "\uac00\ub098",  # Hangul syllables
        "\u0645\u064e\u0631\u0652\u062d\u064e\u0628\u064b\u0627",  # Arabic with its vowel marks
        "\u0661\u0662\u0663",  # Arabic-Indic digits are decimal digits
    ],
)
def test_names_of_letters_digits_and_the_allowed_symbols_are_accepted(name: str) -> None:
    """Any script's letters and decimal digits, with the marks NFC leaves, ``_``, ``-``, space."""
    assert unicodedata.is_normalized("NFC", name)
    assert look_alike_character(name) is None
    assert character_outside_name_allowlist(name) is None
    check_new_name(name, what="Table name")
    check_new_column_name(name)


def test_the_allowed_symbols_are_the_documented_three() -> None:
    """The allowlist is small and documented: underscore, hyphen-minus and the plain space."""
    assert frozenset({"_", "-", " "}) == NAME_SYMBOLS


def test_a_name_not_in_nfc_is_refused_and_the_nfc_form_is_named() -> None:
    """``cafe`` + U+0301 looks like the composed ``caf`` + U+00E9 but compares unequal to it."""
    with pytest.raises(ValidationError) as refused:
        refuse_look_alike_characters(_NFD_CAFE, what="Table name")
    message = str(refused.value)
    assert message.startswith("Table name 'cafe\\u0301' is not in Unicode NFC")
    assert "Use 'caf\\xe9' instead" in message


def test_a_compatibility_singleton_is_refused_with_its_nfc_form() -> None:
    """U+212B ANGSTROM SIGN is a letter, but NFC turns it into U+00C5, which looks the same."""
    with pytest.raises(ValidationError, match=r"is not in Unicode NFC.*Use '\\xc5' instead"):
        refuse_look_alike_characters("\u212b", what="Column name")


@pytest.mark.parametrize(
    ("character", "code"),
    [
        (_CGJ, "U+034F"),
        ("\ufe0f", "U+FE0F"),
        ("\ufe00", "U+FE00"),
        ("\U000e0100", "U+E0100"),
        ("\U000e01ef", "U+E01EF"),
        ("\u180b", "U+180B"),
        ("\u17b4", "U+17B4"),
        ("\u3164", "U+3164"),
        ("\u115f", "U+115F"),
        ("\u1160", "U+1160"),
        ("\uffa0", "U+FFA0"),
        ("\u2800", "U+2800"),
    ],
)
def test_a_character_drawn_as_nothing_is_refused_even_in_nfc(character: str, code: str) -> None:
    """NFC keeps these, so after NFC the name still holds a mark or filler drawn as nothing."""
    name = f"ab{character}c"
    assert unicodedata.is_normalized("NFC", name)
    with pytest.raises(ValidationError) as refused:
        refuse_look_alike_characters(name, what="Column name")
    message = str(refused.value)
    assert f"holds {code} at character 3" in message
    assert "drawn as nothing" in message
    assert character not in message


@pytest.mark.parametrize(
    ("character", "code"),
    [
        ("\xa0", "U+00A0"),
        ("\u1680", "U+1680"),
        ("\u2000", "U+2000"),
        ("\u2007", "U+2007"),
        ("\u200a", "U+200A"),
        ("\u202f", "U+202F"),
        ("\u205f", "U+205F"),
        ("\u3000", "U+3000"),
    ],
)
def test_a_space_other_than_the_plain_one_is_refused(character: str, code: str) -> None:
    """A no-break or wide space looks like U+0020 but is another name."""
    with pytest.raises(ValidationError) as refused:
        refuse_look_alike_characters(f"ab{character}c", what="Table name")
    message = str(refused.value)
    assert f"holds {code} at character 3" in message
    assert "space other than the plain space" in message


@pytest.mark.parametrize(
    ("name", "code", "position"),
    [
        ("\u0301a", "U+0301", 1),  # a mark with nothing before it
        ("a_\u0301", "U+0301", 3),  # a mark on a symbol
        ("1\u20e3", "U+20E3", 2),  # a keycap on a digit
    ],
)
def test_a_combining_mark_without_a_letter_before_it_is_refused(
    name: str, code: str, position: int
) -> None:
    """A mark belongs to a letter; on its own or on a digit or symbol it is not one."""
    assert unicodedata.is_normalized("NFC", name)
    with pytest.raises(ValidationError) as refused:
        refuse_look_alike_characters(name, what="Column name")
    message = str(refused.value)
    assert f"holds {code} at character {position}" in message
    assert "combining mark with no letter before it" in message


@pytest.mark.parametrize(
    ("character", "code"),
    [
        ("$", "U+0024"),
        ("#", "U+0023"),
        ("(", "U+0028"),
        ("\u2160", "U+2160"),  # Roman numeral one: a letter number, not a decimal digit
        ("\xb2", "U+00B2"),  # superscript two
        ("\u2010", "U+2010"),  # hyphen: looks like '-' but is another character
        ("\u2212", "U+2212"),  # minus sign
    ],
)
def test_any_other_character_is_refused_in_a_table_name_and_the_allowed_ones_named(
    character: str, code: str
) -> None:
    """The message names the character and every character such a name may hold."""
    with pytest.raises(ValidationError) as refused:
        refuse_characters_outside_name_allowlist(f"a{character}b", what="Table name")
    message = str(refused.value)
    assert f"holds {code} at character 2" in message
    assert (
        "A new namespace, table or view name may hold only letters, decimal digits, '_', '-' "
        "and the plain space."
    ) in message
    with pytest.raises(ValidationError, match=re.escape(f"holds {code}")):
        check_new_name(f"a{character}b", what="Table name")


@pytest.mark.parametrize(
    "name",
    ["count(*)", "price ($)", "#", "a\\b", "x%", "50 \u2212 10", "area_m\xb2", "\u2160"],
)
def test_a_column_name_may_hold_visible_symbols(name: str) -> None:
    """A query's unaliased expression or a file's header keeps working as a column name."""
    assert look_alike_character(name) is None
    check_new_column_name(name)


def test_check_new_name_applies_the_rule_after_the_earlier_ones() -> None:
    """A hidden character and an outer space keep their own, earlier messages."""
    with pytest.raises(ValidationError, match="U\\+202E"):
        check_new_name("a\u202eb", what="Table name")
    with pytest.raises(ValidationError, match="ends with a space"):
        check_new_name("orders\xa0", what="Table name")
    with pytest.raises(ValidationError, match="NFC"):
        check_new_name(_NFD_CAFE, what="Table name")
    # A symbol is named before the NFC form, so the suggested spelling is one that is allowed.
    with pytest.raises(ValidationError, match="U\\+0024"):
        check_new_name("$cafe\u0301", what="Table name")
    # The braille blank is a symbol, but its look-alike message says more.
    with pytest.raises(ValidationError, match="drawn as nothing"):
        check_new_name("a\u2800", what="Table name")
    with pytest.raises(ValidationError, match="space other than the plain space"):
        check_new_name("a\xa0b", what="Table name")


def test_every_way_of_creating_a_name_refuses_a_look_alike(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table, an external table, a view, a namespace, a rename target and an undrop target."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    for bad, match in ((f"n{_CGJ}", "U\\+034F"), (_NFD_CAFE, "NFC"), ("a\u3164", "U\\+3164")):
        new = _ref(f"gold.{bad}")
        attempts = [
            lambda new=new: catalog.create_table(principal, new, schema=_schema()),
            lambda bad=bad: catalog.create_external_table(
                principal, _ref(f"bronze.{bad}"), location_pattern="raw/x/*.csv", format="csv"
            ),
            lambda new=new: catalog.create_or_replace_view(
                principal, new, sql="SELECT 1 AS id", references=[], columns=[]
            ),
            lambda bad=bad: catalog.create_namespace(
                principal, "dev", NamespacePath.from_parts(["gold", bad])
            ),
            lambda bad=bad: catalog.create_namespace(
                principal, "dev", NamespacePath.from_parts([bad, "daily"])
            ),
            lambda new=new: catalog.rename_table(principal, _ref("gold.orders"), new),
            lambda bad=bad: catalog.undrop_table(principal, _ref("gold.orders"), new_name=bad),
        ]
        for attempt in attempts:
            with pytest.raises(ValidationError, match=match):
                attempt()
    assert catalog.load_table(principal, _ref("gold.orders")).ref.name == "orders"


def test_stored_names_with_look_alikes_keep_working(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """An older release's names are loaded, renamed away and dropped; a child may go under one."""
    catalog.create_table(principal, _ref("gold.old"), schema=_schema())
    catalog.create_table(principal, _ref("gold.gone"), schema=_schema())
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["scratch"]))
    session.execute(update(Table).where(Table.name == "old").values(name=_NFD_CAFE))
    session.execute(update(Table).where(Table.name == "gone").values(name=f"gone{_CGJ}"))
    session.execute(
        update(Namespace).where(Namespace.path == "scratch").values(path="scratch\u3164")
    )
    session.commit()
    stored = _ref(f"gold.{_NFD_CAFE}")

    assert catalog.load_table(principal, stored).ref == stored
    assert catalog.rename_table(principal, stored, _ref("gold.cafe")).ref.name == "cafe"
    catalog.drop_table(principal, _ref(f"gold.gone{_CGJ}"))
    with pytest.raises(NotFoundError):
        catalog.load_table(principal, _ref(f"gold.gone{_CGJ}"))
    nested = NamespacePath.from_parts(["scratch\u3164", "daily"])
    catalog.create_namespace(principal, "dev", nested)
    catalog.drop_namespace(principal, "dev", nested)
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["scratch\u3164"]))


def test_a_new_column_with_a_look_alike_is_refused_everywhere(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A new table's column, a write's new column, a view's and an external table's."""
    catalog.create_table(
        principal,
        _ref("gold.symbols"),
        schema=Schema(
            NestedField(1, "count(*)", LongType(), required=False),
            NestedField(2, "price ($)", LongType(), required=False),
        ),
    )
    with pytest.raises(ValidationError, match=r"Column name 'id\\u034f' holds U\+034F"):
        catalog.create_table(
            principal,
            _ref("gold.items"),
            schema=Schema(NestedField(1, f"id{_CGJ}", LongType(), required=False)),
        )
    with pytest.raises(ValidationError, match="NFC"):
        _write(catalog, principal, "gold.fresh", WriteMode.CREATE, pa.table({_NFD_CAFE: [1]}))
    with pytest.raises(ValidationError, match="U\\+00A0"):
        catalog.create_or_replace_view(
            principal,
            _ref("gold.v"),
            sql="SELECT 1 AS x",
            references=[],
            columns=[ColumnSpec(name="a\xa0b", type="int", nullable=True)],
        )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id\ufe0f": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    with pytest.raises(ValidationError, match="U\\+FE0F"):
        catalog.create_external_table(
            principal,
            TableRef.from_dotted("dev", "raw.events"),
            location_pattern="raw/events/*.parquet",
            format="parquet",
        )


def test_an_added_schema_refuses_a_new_look_alike_column_and_keeps_a_stored_one() -> None:
    """A schema change through the Iceberg REST catalog or a write: only new names are judged."""
    base = _metadata_with_column(_NFD_CAFE)
    kept = Schema(
        NestedField(1, _NFD_CAFE, LongType(), required=False),
        NestedField(2, "plain", LongType(), required=False),
        schema_id=1,
    )
    apply_updates(base, [AddSchemaUpdate(schema_=kept)])
    added = Schema(
        NestedField(1, _NFD_CAFE, LongType(), required=False),
        NestedField(2, "new\u2800", LongType(), required=False),
        schema_id=1,
    )
    with pytest.raises(ValidationError, match=r"Column name 'new\\u2800' holds U\+2800"):
        apply_updates(base, [AddSchemaUpdate(schema_=added)])
