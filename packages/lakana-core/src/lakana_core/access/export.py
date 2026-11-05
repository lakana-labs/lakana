"""Write the live access state as an access file (#46)."""

from __future__ import annotations

import re
from collections.abc import Mapping

import yaml

from lakana_core.access.model import (
    AccessState,
    CiTrustEntry,
    ColumnMaskEntry,
    GrantEntry,
    GroupEntry,
    MembershipEntry,
    RowFilterEntry,
    Scopes,
    ServiceAccountEntry,
    Target,
)

_HEADER = "# Lakana access file. Review changes in a pull request; plan with lakana access plan.\n"
_BOOL_TAG = "tag:yaml.org,2002:bool"


class _Yaml12Dumper(yaml.SafeDumper):
    """A dumper that quotes only the words the access file reads as booleans.

    PyYAML writes ``'on'`` and ``'no'`` quoted because YAML 1.1 reads them as booleans; the
    access file reads only ``true`` and ``false`` so, and a quoted ``on:`` would be noise.
    """


_Yaml12Dumper.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeDumper.yaml_implicit_resolvers.items()
}
_Yaml12Dumper.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _target_key(target: Target) -> str:
    """Name the key a grant's target is written under.

    Parameters
    ----------
    target
        The grant's target.

    Returns
    -------
    str
        ``table`` for a table, ``on`` for every other tier.
    """
    return "table" if target.table else "on"


def _ci_rule(entry: CiTrustEntry) -> dict[str, object]:
    """Write a CI trust rule, leaving its defaults out.

    Parameters
    ----------
    entry
        The rule.

    Returns
    -------
    dict
        The rule's keys in file order.
    """
    rule: dict[str, object] = {
        "issuer": entry.issuer,
        "repository": entry.repository,
        "as": f"service:{entry.as_}",
        "on": entry.on,
        "conditions": dict(entry.conditions),
    }
    if entry.kinds != ("deploy",):
        rule["kinds"] = list(entry.kinds)
    if not entry.enabled:
        rule["enabled"] = False
    if entry.start_on_deploy_jobs:
        rule["start_on_deploy_jobs"] = True
    return rule


def export_document(state: AccessState, scopes: Scopes) -> str:
    """Write the entries of some scopes as an access file.

    Defaults are left out (`kinds: [deploy]`, `enabled: true`, `start_on_deploy_jobs: false`),
    entries are sorted by `sort_key`, and the text starts with a comment saying how to review
    and plan it; ``manages`` names the scopes.

    Parameters
    ----------
    state
        The live state.
    scopes
        The scopes to export.

    Returns
    -------
    str
        The file's text; `parse_access_file` reads it back to `state.owned(scopes)`.
    """
    owned = state.owned(scopes)
    document: dict[str, object] = {"version": 1, "manages": sorted(scopes.names)}
    members: dict[str, list[str]] = {group.name: [] for group in owned.of_type(GroupEntry)}
    for member in owned.of_type(MembershipEntry):
        members[member.group].append(member.member)
    if members:
        document["groups"] = {
            name: {"members": sorted(found)} for name, found in sorted(members.items())
        }
    accounts = [entry.name for entry in owned.of_type(ServiceAccountEntry)]
    if accounts:
        document["service_accounts"] = sorted(accounts)
    grants = [
        {
            "to": str(entry.to),
            **({"role": entry.role} if entry.role else {"action": entry.action}),
            _target_key(entry.on): str(entry.on),
        }
        for entry in owned.of_type(GrantEntry)
    ]
    if grants:
        document["grants"] = grants
    policies: list[Mapping[str, object]] = [
        {"kind": "row_filter", "table": str(e.table), "expression": e.expression, "to": str(e.to)}
        for e in owned.of_type(RowFilterEntry)
    ]
    policies += [
        {
            "kind": "column_mask",
            "table": str(e.table),
            "column": e.column,
            "expression": e.expression,
            "to": str(e.to),
        }
        for e in owned.of_type(ColumnMaskEntry)
    ]
    if policies:
        document["policies"] = policies
    rules = [_ci_rule(entry) for entry in owned.of_type(CiTrustEntry)]
    if rules:
        document["ci_trust"] = rules
    body = yaml.dump(
        document, Dumper=_Yaml12Dumper, sort_keys=False, allow_unicode=True, width=4096
    )
    return _HEADER + body
