"""The lockout guard of an access file: never leave the instance without an administrator (#46).

R7: a file that leaves no active instance ADMIN is always refused; ``--allow-lockout`` only
lets a caller drop their own ADMIN role while another administrator remains.
"""

from __future__ import annotations

from lakana_core.access.model import AccessState, GrantEntry, MembershipEntry


def admins_after(state: AccessState, active: frozenset[str]) -> frozenset[str]:
    """Name the active principals that hold the instance ADMIN role in a state.

    Parameters
    ----------
    state
        The whole instance's access after an apply: the file's entries plus the live entries
        outside its scopes.
    active
        The names of the principals active after the apply.

    Returns
    -------
    frozenset of str
        Holders of ADMIN on the instance, directly or through a group, that are active.
    """
    holders: set[str] = set()
    members: dict[str, set[str]] = {}
    for entry in state.entries:
        if isinstance(entry, MembershipEntry):
            members.setdefault(entry.group, set()).add(entry.member)
    for entry in state.entries:
        if isinstance(entry, GrantEntry) and entry.role == "ADMIN" and entry.on.environment is None:
            if entry.to.kind == "group":
                holders |= members.get(entry.to.name, set())
            else:
                holders.add(entry.to.name)
    return frozenset(holders & active)


def lockout_problems(
    *, file_name: str, after: frozenset[str], caller: str, allow_lockout: bool
) -> list[str]:
    """Refuse a file that leaves no administrator, or drops the caller's own ADMIN role.

    Parameters
    ----------
    file_name
        The base name the messages use.
    after
        The active instance administrators after the apply (`admins_after`).
    caller
        The name of the principal planning or applying.
    allow_lockout
        Whether the caller accepts losing their own ADMIN role.

    Returns
    -------
    list of str
        No problem, or the one that applies.
    """
    if not after:
        return [
            f"{file_name}: the file leaves no active ADMIN on the instance; keep "
            f"{{to: user:{caller}, role: ADMIN, on: instance}} or give ADMIN to another active "
            "person"
        ]
    if caller not in after and not allow_lockout:
        return [
            f"{file_name}: the file removes your own ADMIN role on the instance (MANAGE_GRANT); "
            "keep it, or repeat with --allow-lockout if another administrator applies from now on"
        ]
    return []
