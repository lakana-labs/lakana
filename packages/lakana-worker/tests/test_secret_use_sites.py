"""Every place that decrypts a secret checks the secret's use first."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGES = Path(__file__).resolve().parents[2]
_SITES = {
    "lakana-core/src/lakana_core/connections/fetch.py": "connection_secret",
    "lakana-core/src/lakana_core/storage/resolver.py": "_resolve_secret_values",
    "lakana-worker/src/lakana_worker/deploy.py": "resolve_credential",
    "lakana-worker/src/lakana_worker/index_credentials.py": "resolve_index_access",
    "lakana-worker/src/lakana_worker/notify.py": "_webhook",
}
_CRYPTO = "lakana-core/src/lakana_core/secrets/crypto.py"


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Find the calls of a function by its name.

    Parameters
    ----------
    node
        The tree to search.
    name
        The called function's name, as a bare name or an attribute.

    Returns
    -------
    list of ast.Call
        The calls, in source order.
    """
    found = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and name in (getattr(call.func, "id", None), getattr(call.func, "attr", None))
    ]
    return sorted(found, key=lambda call: (call.lineno, call.col_offset))


_DECRYPTERS = {"decrypt_secret_row", "decrypt_secret_values", "unwrap"}
_BROAD = {"Exception", "BaseException"}
_ALLOWED_HOMES = {_CRYPTO, "lakana-core/src/lakana_core/secrets/keys.py"}


def _names_a_decrypter(source: str) -> bool:
    """Say whether code refers to a function that opens a secret, however it is imported.

    Parameters
    ----------
    source
        Python source.

    Returns
    -------
    bool
        Whether a name, an attribute or an import (also under an alias) is one of the
        decrypting functions; words in comments and docstrings do not count.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in _DECRYPTERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _DECRYPTERS:
            return True
        if isinstance(node, ast.alias) and node.name.split(".")[-1] in _DECRYPTERS:
            return True
    return False


def _is_broad(kind: ast.expr | None) -> bool:
    """Say whether an `except` clause catches every exception.

    Parameters
    ----------
    kind
        The handler's type expression; `None` for a bare `except`.

    Returns
    -------
    bool
        Whether it is bare, names `Exception` or `BaseException` (also as `builtins.Exception`),
        or is a tuple that contains one of them.
    """
    if kind is None:
        return True
    if isinstance(kind, ast.Tuple):
        return any(_is_broad(element) for element in kind.elts)
    return getattr(kind, "id", getattr(kind, "attr", None)) in _BROAD


def _under_broad_handler(tree: ast.AST, call: ast.Call) -> bool:
    """Say whether a call sits in a `try` body whose handler catches every exception.

    Parameters
    ----------
    tree
        The function's tree.
    call
        The call.

    Returns
    -------
    bool
        Whether a broad `except` could swallow or relabel what the call raises (the refusal
        and the environment-mismatch `ValueError` must reach the caller).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not any(call in ast.walk(stmt) for stmt in node.body):
            continue
        if any(_is_broad(handler.type) for handler in node.handlers):
            return True
    return False


def test_only_the_known_sites_decrypt_a_secret() -> None:
    """Another caller of any decrypting function would read a secret for any use."""
    callers = sorted(
        str(path.relative_to(_PACKAGES))
        for path in _PACKAGES.glob("*/src/**/*.py")
        if str(path.relative_to(_PACKAGES)) not in _ALLOWED_HOMES
        and _names_a_decrypter(path.read_text(encoding="utf-8"))
    )

    assert callers == sorted(_SITES)


@pytest.mark.parametrize(
    "source",
    [
        "decrypt_secret_values(a, b)",
        "from lakana_core.secrets.crypto import decrypt_secret_row as d\nd(a)",
        "import lakana_core.secrets.crypto as c\nc.decrypt_secret_row(a)",
        "provider.unwrap(data, aad)",
    ],
)
def test_the_scan_finds_each_way_to_open_a_secret(source: str) -> None:
    """The inventory test would otherwise miss another site written this way."""
    assert _names_a_decrypter(source)


def test_the_scan_ignores_words_in_docstrings_and_comments() -> None:
    """Prose that mentions `unwrap` is not a caller."""
    assert not _names_a_decrypter('"""It must unwrap the key."""\n# decrypt_secret_row\nx = 1')


@pytest.mark.parametrize(
    "handler",
    ["", "Exception", "BaseException", "(ValueError, Exception)", "builtins.Exception"],
)
def test_the_guard_finds_each_broad_handler(handler: str) -> None:
    """A refusal raised inside such a `try` would be swallowed or relabelled."""
    tree = ast.parse(
        f"try:\n    require_use()\nexcept {handler}:\n    pass\n".replace("except :", "except:")
    )
    (call,) = _calls(tree, "require_use")

    assert _under_broad_handler(tree, call)


def test_the_guard_accepts_a_narrow_handler() -> None:
    """Catching only the refusal is how `resolve_index_access` relabels it."""
    tree = ast.parse("try:\n    require_use()\nexcept (SecretUseRefused, KeyError):\n    pass\n")
    (call,) = _calls(tree, "require_use")

    assert not _under_broad_handler(tree, call)


@pytest.mark.parametrize(("path", "function"), sorted(_SITES.items()))
def test_each_decrypt_site_requires_the_use_before_it_decrypts(path: str, function: str) -> None:
    """`require_use` runs first and outside any broad `except`, so its refusal is not swallowed."""
    tree = ast.parse((_PACKAGES / path).read_text(encoding="utf-8"))
    (site,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    ]

    required, decrypted = _calls(site, "require_use"), _calls(site, "decrypt_secret_row")

    assert len(decrypted) == 1
    assert len(required) >= 1
    assert required[0].lineno < decrypted[0].lineno
    assert not any(_under_broad_handler(site, call) for call in required)
