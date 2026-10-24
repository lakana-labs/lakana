"""A ``parametrize`` value must be the same in every process.

``pytest -n`` collects the tests in each worker and the controller refuses a run whose
workers did not collect identical test ids. A value drawn at import time (``uuid4()``, a
random number, the clock, the process id) makes the ids differ between processes, so
collection fails in parallel while a serial run passes. This test reads every test file
and names the line. Import aliases (``import uuid as u``, ``from uuid import uuid4 as u4``,
``from pytest import mark``, ``import pytest as pt``) are resolved.

Not covered (false negatives): an unstable value built elsewhere and passed by name (a
module-level list or a helper's result), ``metafunc.parametrize`` in ``pytest_generate_tests``,
and fixture ``params=``. Keep those deterministic by hand.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_ANY_CALL_ON = ("random", "secrets")
_EXACT = frozenset(
    {
        "uuid.uuid1",
        "uuid.uuid4",
        "uuid1",
        "uuid4",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "datetime.now",
        "datetime.utcnow",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "date.today",
        "datetime.date.today",
        "os.getpid",
    }
)


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map every imported local name to what it stands for.

    Parameters
    ----------
    tree
        The parsed module.

    Returns
    -------
    dict
        ``{"u": "uuid", "u4": "uuid.uuid4", "mark": "pytest.mark"}`` for
        ``import uuid as u``, ``from uuid import uuid4 as u4``, ``from pytest import mark``.
    """
    known: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    known[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                known[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return known


def _dotted(node: ast.expr, known: dict[str, str] | None = None) -> str | None:
    """Spell a name or attribute chain as a dotted string, import aliases resolved.

    Parameters
    ----------
    node
        The expression.
    known
        Local names to what they stand for, from `_aliases`.

    Returns
    -------
    str or None
        ``a.b.c`` for names and attributes; `None` for anything else.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = (known or {}).get(node.id, node.id)
    return ".".join([head, *reversed(parts)])


def _is_parametrize(call: ast.Call, known: dict[str, str]) -> bool:
    """Tell whether a call is ``pytest.mark.parametrize(...)`` (or ``mark.parametrize``).

    Parameters
    ----------
    call
        The call.
    known
        Local names to what they stand for, from `_aliases`.

    Returns
    -------
    bool
        Whether the callee ends in ``mark.parametrize``.
    """
    name = _dotted(call.func, known)
    return name is not None and name.endswith("mark.parametrize")


def _is_unstable(call: ast.Call, known: dict[str, str]) -> bool:
    """Tell whether a call yields a different value in every process.

    Parameters
    ----------
    call
        The call.
    known
        Local names to what they stand for, from `_aliases`.

    Returns
    -------
    bool
        Whether it is a random, clock or process-id call.
    """
    name = _dotted(call.func, known)
    if name is None:
        return False
    return name in _EXACT or (name.split(".")[0] in _ANY_CALL_ON and "." in name)


def unstable_parametrize_values(source: str, filename: str) -> list[str]:
    """Find ``parametrize`` arguments that are drawn afresh in every process.

    Parameters
    ----------
    source
        The Python source of a test file.
    filename
        Its name, for the messages.

    Returns
    -------
    list of str
        One message per offending call, naming file and line and the reason.
    """
    found: list[str] = []
    tree = ast.parse(source)
    known = _aliases(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_parametrize(node, known)):
            continue
        ids = next((k.value for k in node.keywords if k.arg == "ids"), None)
        if isinstance(ids, ast.List | ast.Tuple):
            # Fixed ids keep the collected test ids identical whatever the values are.
            arguments: list[ast.expr] = [ids]
        else:
            arguments = [*node.args, *(k.value for k in node.keywords)]
        for argument in arguments:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Call) and _is_unstable(inner, known):
                    found.append(
                        f"{filename}:{inner.lineno}: parametrize uses "
                        f"{_dotted(inner.func, known)}(), "
                        "which differs in every process. Parallel workers (pytest -n) must "
                        "collect identical test ids: use a fixed literal, or ids=[...]."
                    )
    return found


def test_the_guard_flags_a_value_drawn_at_import_time() -> None:
    """A ``uuid4()`` inside a ``parametrize`` list is reported with file and line."""
    source = (
        "import uuid\nimport pytest\n\n"
        "@pytest.mark.parametrize('v', [uuid.uuid4().bytes, 1])\n"
        "def test_a(v): ...\n\n"
        "@pytest.mark.parametrize('v', [1], ids=[str(__import__('os').getpid())])\n"
        "def test_b(v): ...\n"
        "@pytest.mark.parametrize('v', [random.random(), datetime.now(), os.getpid()])\n"
        "def test_c(v): ...\n"
        "@pytest.mark.parametrize('v', [uuid4()], ids=['fixed'])\n"
        "def test_d(v): ...\n"
    )
    messages = unstable_parametrize_values(source, "t.py")
    assert any(m.startswith("t.py:4: parametrize uses uuid.uuid4()") for m in messages)
    assert any("random.random()" in m for m in messages)
    assert any("datetime.now()" in m for m in messages)
    assert any("os.getpid()" in m for m in messages)
    assert all("identical test ids" in m for m in messages)
    assert not any(m.startswith("t.py:11") for m in messages)  # fixed ids make test_d safe


@pytest.mark.parametrize(
    "header",
    [
        "import uuid as u\nimport pytest\n",
        "from uuid import uuid4 as u4\nimport pytest\n",
        "from pytest import mark\nimport uuid\n",
        "import pytest as pt\nimport uuid\n",
        "from pytest import mark as m\nfrom uuid import uuid4\n",
        "import time as t\nimport pytest\n",
        "from os import getpid as pid\nimport pytest\n",
    ],
    ids=["module", "function", "mark", "pytest", "mark-as", "time", "getpid"],
)
def test_the_guard_resolves_import_aliases(header: str) -> None:
    """An aliased module, function, ``mark`` or ``pytest`` is still recognised."""
    decorator = {
        "import uuid as u": "pytest.mark.parametrize('v', [u.uuid4()])",
        "from uuid import uuid4 as u4": "pytest.mark.parametrize('v', [u4()])",
        "from pytest import mark\nimport uuid": "mark.parametrize('v', [uuid.uuid4()])",
        "import pytest as pt": "pt.mark.parametrize('v', [uuid.uuid4()])",
        "from pytest import mark as m": "m.parametrize('v', [uuid4()])",
        "import time as t": "pytest.mark.parametrize('v', [t.time()])",
        "from os import getpid as pid": "pytest.mark.parametrize('v', [pid()])",
    }
    key = next(k for k in decorator if header.startswith(k))
    source = f"{header}\n@{decorator[key]}\ndef test_a(v): ...\n"
    messages = unstable_parametrize_values(source, "t.py")
    assert len(messages) == 1, source
    assert messages[0].startswith("t.py:")


def test_the_guard_passes_fixed_values_and_unrelated_calls() -> None:
    """Literals, a constant UUID, a ``time(1, 2, 3)`` value and a random call outside are fine."""
    source = (
        "import uuid, random\nimport pytest\n\n"
        "_X = random.random()\n"
        "@pytest.mark.parametrize('v', [uuid.UUID('12345678-1234-5678-1234-567812345678').bytes,"
        " time(1, 2, 3), 2**31 - 1, f()()])\n"
        "def test_a(v):\n    uuid.uuid4()\n"
    )
    assert unstable_parametrize_values(source, "t.py") == []


def _test_files() -> list[Path]:
    """List every test file of the four packages and of ``scripts``.

    Returns
    -------
    list of Path
        Every Python file under a ``tests`` directory, sorted.
    """
    roots = [*ROOT.glob("packages/*/tests"), ROOT / "scripts" / "tests"]
    return sorted(path for root in roots for path in root.rglob("*.py"))


@pytest.mark.slow
def test_no_parametrize_value_differs_between_processes() -> None:
    """No test file draws a ``parametrize`` value from a random source or the clock."""
    problems = [
        message
        for path in _test_files()
        for message in unstable_parametrize_values(
            path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))
        )
    ]
    assert problems == []
