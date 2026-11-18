"""Compare the interface's roles and actions with the server's and with the reference.

`ui/src/api/grants.ts` lists the five roles and every action as literal arrays (`ROLES`,
`ACTIONS`), which the Grants filter and Add grant offer in that order. A role or an action the
server adds, removes or reorders that the list does not follow fails here. The check lives in
Python because the interface's own tests run on a copy of `ui/` alone (`prove-gates`), where
neither the server nor `docs/` is.
"""

import json
import re
from pathlib import Path

import pytest

from lakana_core.auth import Action
from lakana_core.grants.roles import Role

ROOT = Path(__file__).resolve().parents[2]
GRANTS_TS = (ROOT / "ui/src/api/grants.ts").read_text()
OPENAPI = json.loads((ROOT / "docs/site/reference/openapi.json").read_text())


def _ui_list(name: str) -> list[str]:
    """Read one of the literal arrays of `grants.ts`.

    Parameters
    ----------
    name
        The constant's name, such as ``ROLES``.

    Returns
    -------
    list of str
        Its strings, in order.
    """
    found = re.search(rf"export const {name} = \[(.*?)\] as const;", GRANTS_TS, re.DOTALL)
    assert found is not None, f"ui/src/api/grants.ts has no `export const {name} = [...] as const`"
    return re.findall(r'"([A-Z_]+)"', found.group(1))


@pytest.mark.parametrize(
    ("name", "server", "schema"),
    [
        ("ROLES", [role.value for role in Role], "Role"),
        ("ACTIONS", [action.value for action in Action], "Action"),
    ],
)
def test_the_interface_knows_every_role_and_action_in_the_servers_order(
    name: str, server: list[str], schema: str
) -> None:
    ui = _ui_list(name)
    assert ui == server, f"ui/src/api/grants.ts {name} differs from the server's {schema}"
    assert ui == OPENAPI["components"]["schemas"][schema]["enum"]
