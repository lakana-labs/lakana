"""The layer `lakana load` writes into, and the one place its rule is checked.

`lakana load` is a person loading a local file once, for exploration, testing or teaching;
anything that runs again belongs in an asset. So a load lands in the raw layer, `bronze`, and
nowhere else: the target's first namespace segment must be `bronze` (any deeper `bronze.…`
namespace too). The server's load route checks it before it reads a byte of the body, so the
CLI, the SDK and a plain HTTP client get the same refusal. The layers are fixed
(`lakana_core.projects.layers.LAYERS`, #252), so a table's layer is its first namespace segment and
this stays a name rule.
"""

from __future__ import annotations

from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.projects.layers import LAYERS

LOAD_LAYER = LAYERS[0]
"""The first namespace segment every load target must have: the first of `LAYERS`, bronze."""


def require_bronze(ref: TableRef) -> None:
    """Refuse a load target outside the bronze layer.

    Parameters
    ----------
    ref
        The table the load would write.

    Raises
    ------
    ValidationError
        If the table's first namespace segment is not `bronze`; the message says to load into
        `bronze.<name>` and transform it into the target with an asset.
    """
    if ref.namespace.parts[0] == LOAD_LAYER:
        return
    raise ValidationError(
        f"`lakana load` loads into bronze only: load into `{LOAD_LAYER}.{shown(ref.name)}` and "
        f"transform it into `{shown(ref.dotted())}` with an asset."
    )
