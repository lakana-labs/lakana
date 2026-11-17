"""Compare the UI's mirror of the connection kinds with the server's and with the reference.

The server has no public list of each kind's fields, so this test reads the private names of
`lakana_core.connections.kinds` (`_MODELS`, `_AUTH_FIELDS` and the others): a change there that
the mirror in `ui/src/connections/kinds.mirror.json` does not follow fails here.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal, get_args, get_origin

import pytest

from lakana_core.connections import kinds as server
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.models.connection import ConnectionTestOutcome

ROOT = Path(__file__).resolve().parents[2]
MIRROR: dict[str, Any] = json.loads((ROOT / "ui/src/connections/kinds.mirror.json").read_text())
REFERENCE = (ROOT / "docs/site/reference/connections.md").read_text()


def _first_column(heading_line: str) -> list[str]:
    """Read the first column of the table that starts with a given header row.

    Parameters
    ----------
    heading_line
        The start of the table's header row, such as ``| Outcome |``.

    Returns
    -------
    list of str
        The first cell of each body row, without its backticks, in order.
    """
    lines = REFERENCE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_line))
    cells: list[str] = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells.append(line.split("|")[1].strip().strip("`"))
    return cells


def _literal_choices(annotation: object) -> list[str] | None:
    """Read the values of a field's `Literal` type, looking through `Optional`.

    Parameters
    ----------
    annotation
        A model field's annotation.

    Returns
    -------
    list of str or None
        The literal's values, or ``None`` when the field is not a literal.
    """
    for arm in (annotation, *get_args(annotation)):
        if get_origin(arm) is Literal:
            return list(get_args(arm))
    return None


def _mirror_secret(kind: str, auth: str | None) -> tuple[set[str], set[str]]:
    """Derive the required and optional secret keys the mirror states.

    Parameters
    ----------
    kind
        The kind.
    auth
        The ``http`` auth, or ``None``.

    Returns
    -------
    tuple of (set, set)
        Required keys, optional keys; `azure`'s one-of keys are optional, `tcp`'s are free.
    """
    spec = MIRROR["kinds"][kind]["secret"]
    if spec is None or "free" in spec:
        return set(), set()
    if "byAuth" in spec:
        key = MIRROR["httpSecretKey"].get(auth)
        return ({key} if key else set()), set()
    if "oneOf" in spec:
        return set(), set(spec["oneOf"])
    return set(spec["required"]), set(spec["optional"])


def test_the_ui_lists_every_kind_of_the_server_once() -> None:
    """The mirror's `order` holds each server kind exactly once."""
    order = MIRROR["order"]
    assert len(order) == len(set(order))
    assert set(order) == {k.value for k in ConnectionKind}
    assert set(MIRROR["kinds"]) == set(order)


@pytest.mark.parametrize("kind", list(ConnectionKind))
def test_each_kinds_fields_and_required_ones_match_the_servers_models(
    kind: ConnectionKind,
) -> None:
    """Each part's keys and which of them are required equal the server model's.

    Parameters
    ----------
    kind
        The kind.
    """
    models = server._MODELS[kind]
    for part, model in zip(("address", "settings"), models, strict=True):
        mirrored = {f["key"]: f["required"] for f in MIRROR["kinds"][kind.value][part]}
        actual = {name: field.is_required() for name, field in model.model_fields.items()}
        assert mirrored == actual, f"{kind.value} {part}"
        choices = {f["key"]: f.get("choices") for f in MIRROR["kinds"][kind.value][part]}
        literals = {name: _literal_choices(f.annotation) for name, f in model.model_fields.items()}
        assert choices == literals, f"{kind.value} {part} choices"


def test_secret_keys_match_for_every_kind_and_auth() -> None:
    """The mirror's secret keys equal `secret_keys` for every kind, and every auth of `http`."""
    cases = [(k, {}, None) for k in ConnectionKind if k is not ConnectionKind.HTTP]
    cases += [(ConnectionKind.HTTP, {"auth": a}, a) for a in server._AUTH_FIELDS]
    for kind, settings, auth in cases:
        required, optional = server.secret_keys(kind, settings)
        assert _mirror_secret(kind.value, auth) == (set(required), set(optional)), (kind, auth)
        assert server.takes_secret(kind, settings) == (
            kind is ConnectionKind.TCP or any(_mirror_secret(kind.value, auth))
        )
    assert MIRROR["kinds"]["tcp"]["secret"] == {"free": True, "max": 16}


def test_http_auth_fields_match() -> None:
    """The mirror's auth setting keys and required ones equal the server's."""
    assert {a: set(v) for a, v in MIRROR["httpAuth"].items()} == {
        a: set(v) for a, v in server._AUTH_FIELDS.items()
    }
    assert {a: tuple(v) for a, v in MIRROR["httpAuthRequired"].items()} == server._AUTH_REQUIRED


def test_what_moves_a_secret_matches() -> None:
    """The fields that move a secret, and the order of TLS strength, equal the server's."""
    moves = MIRROR["movesSecret"]
    assert tuple(moves["address"]) == server._ADDRESS_MOVING
    assert tuple(moves["settings"]) == server._SETTINGS_MOVING
    assert moves["tlsStrength"] == sorted(
        server._TLS_STRENGTH, key=server._TLS_STRENGTH.__getitem__
    )


def test_outcomes_match_the_server_and_the_reference() -> None:
    """The mirror's outcomes are the server's, in the reference table's order."""
    assert set(MIRROR["outcomes"]) == {o.value for o in ConnectionTestOutcome}
    assert len(MIRROR["outcomes"]) == len(set(MIRROR["outcomes"]))
    assert MIRROR["outcomes"] == _first_column("| Outcome |")


def test_the_kinds_follow_the_reference_tables_order() -> None:
    """The mirror's `order` is the reference's "Kinds" table's first column."""
    assert MIRROR["order"] == _first_column("| Kind |")


def test_limits_match() -> None:
    """The mirror's byte limits equal the server's."""
    assert MIRROR["limits"] == {
        "caBytes": server.MAX_CA_BYTES,
        "secretValueBytes": server.MAX_SECRET_VALUE_BYTES,
    }


def test_the_defaults_match_the_reference() -> None:
    """The ports and TLS default the mirror states are the ones the reference names."""
    ports = {
        k: next(f["default"] for f in MIRROR["kinds"][k]["address"] if f["key"] == "port")
        for k in ("postgresql", "mariadb")
    }
    assert ports == {"postgresql": 5432, "mariadb": 3306}
    assert re.search(r"`postgresql` \| `host`, `port` \(default `5432`\)", REFERENCE)
    assert re.search(r"`mariadb` \| `host`, `port` \(default `3306`\)", REFERENCE)
