"""Tests for the access file's model and strict parser (#46)."""

from __future__ import annotations

import pytest

from lakana_core.access import file as access_file
from lakana_core.access.file import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    AccessFileError,
    parse_access_file,
    validate_file_name,
)
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
    Subject,
    Target,
    describe,
    entry_json,
    fingerprint,
    sort_key,
)
from lakana_core.auth import SecurableKind
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath

_FULL = """\
version: 1
manages: [instance]
groups:
  finance: {members: [alice, bob]}
  empty: {members: []}
service_accounts: [etl-bot]
grants:
  - {to: group:finance, role: ANALYST, on: prod}
  - {to: service:etl-bot, action: WRITE_TABLE, on: prod.gold.sales}
  - {to: user:alice, action: READ_TABLE, table: prod.gold.sales.orders}
policies:
  - {kind: column_mask, table: prod.gold.customers, column: email, expression: "'***'",
     to: group:finance}
  - {kind: row_filter, table: prod.gold.orders, expression: "region = 'EU'", to: user:bob}
ci_trust:
  - {issuer: github, repository: etl, as: service:etl-bot, on: prod,
     conditions: {ref: refs/heads/main}}
"""


def _problems(text: str) -> list[str]:
    with pytest.raises(AccessFileError) as caught:
        parse_access_file(text, file_name="access.yaml")
    return list(caught.value.problems)


def test_a_full_file_parses_into_entries_with_their_paths() -> None:
    """Every section becomes entries; a path names each; defaults fill CI trust."""
    parsed = parse_access_file(_FULL, file_name="access.yaml")
    assert parsed.manages == ("instance",)
    finance = Subject("group", "finance")
    assert dict((e, p) for p, e in parsed.entries) == {
        GroupEntry("finance"): "groups.finance",
        GroupEntry("empty"): "groups.empty",
        MembershipEntry("finance", "alice"): "groups.finance.members[0]",
        MembershipEntry("finance", "bob"): "groups.finance.members[1]",
        ServiceAccountEntry("etl-bot"): "service_accounts[0]",
        GrantEntry(finance, "ANALYST", None, Target("prod")): "grants[0]",
        GrantEntry(
            Subject("service", "etl-bot"), None, "WRITE_TABLE", Target("prod", ("gold", "sales"))
        ): "grants[1]",
        GrantEntry(
            Subject("user", "alice"),
            None,
            "READ_TABLE",
            Target("prod", ("gold", "sales"), "orders"),
        ): "grants[2]",
        ColumnMaskEntry(
            finance, Target("prod", ("gold",), "customers"), "email", "'***'"
        ): "policies[0]",
        RowFilterEntry(
            Subject("user", "bob"), Target("prod", ("gold",), "orders"), "region = 'EU'"
        ): "policies[1]",
        CiTrustEntry(
            "github", "etl", "etl-bot", "prod", (("ref", "refs/heads/main"),)
        ): "ci_trust[0]",
    }
    assert len(parsed.sha256) == 64


_CI = "issuer: g, repository: r, as: service:a, on: prod, conditions: {ref: m}"
_INST = "version: 1\nmanages: [instance]\n"
_PRE = "version: 1\nmanages: [prod]\n"
_EXPR = "table: prod.g.t, expression: x"


def _one(section: str, item: str, manages: str = "prod") -> str:
    """Build a file with one entry in one list section.

    Parameters
    ----------
    section
        The section's key.
    item
        The entry as YAML flow text.
    manages
        The ``manages`` list's content.

    Returns
    -------
    str
        The file.
    """
    return f"version: 1\nmanages: [{manages}]\n{section}:\n  - {{{item}}}\n"


def _grant(item: str) -> str:
    return _one("grants", item, "instance")


def _policy(item: str) -> str:
    return _one("policies", item)


def _ci(item: str) -> str:
    return _one("ci_trust", item)


def _at(where: str, line: int, message: str) -> str:
    return f"access.yaml: {where} (line {line}): {message}"


_STRICT = "must be 1 to 255 printable characters without blanks at either end; rename it"
_SUBJECT_KINDS = "the kinds are user, group, service"
_CI_ERR = "access.yaml: ci_trust[0]"


_CASES = [
    (
        _INST + "extra: 1\n",
        _at(
            "extra",
            3,
            "unknown key; the allowed keys are version, manages, groups, service_accounts, "
            "grants, policies, ci_trust",
        ),
    ),
    (
        "version: 2\nmanages: [instance]\n",
        _at("version", 1, "version 2 is not supported; write version: 1"),
    ),
    (
        "version: 1\n",
        "access.yaml: manages: missing; write manages: [instance] or a list of environments",
    ),
    (
        "version: 1\nmanages: [instance, prod]\n",
        _at(
            "manages",
            2,
            "instance already covers every environment; write manages: [instance] or list "
            "environments only",
        ),
    ),
    (
        _PRE + "service_accounts: [bot]\n",
        _at(
            "service_accounts",
            3,
            "service accounts exist instance-wide; add instance to manages, or leave "
            "service_accounts out and create the account with lakana principal create",
        ),
    ),
    (
        _grant("to: user:a, role: VIEWER, on: staging").replace("[instance]", "[prod]"),
        _at(
            "grants[0]",
            4,
            "staging is outside manages (prod); add staging to manages or remove the entry",
        ),
    ),
    (
        _grant("to: alice, role: VIEWER, on: prod"),
        _at(
            "grants[0].to",
            4,
            "'alice' names no kind; write user:alice, group:alice or service:alice",
        ),
    ),
    (
        _grant("to: user:a, role: VIEWER, action: READ_TABLE, on: prod"),
        _at("grants[0]", 4, "give exactly one of role or action"),
    ),
    (
        _grant("to: user:a, role: OWNER, on: prod"),
        _at(
            "grants[0].role",
            4,
            "unknown role 'OWNER'; the roles are VIEWER, ANALYST, DATA_SCIENTIST, "
            "DATA_ENGINEER, ADMIN",
        ),
    ),
    (
        _grant("to: user:a, role: VIEWER, on: prod") + "  - {to: user:a, role: VIEWER, on: prod}\n",
        _at("grants[1]", 5, "repeats grants[0]; remove one"),
    ),
    (
        _policy("kind: mask, to: user:a, " + _EXPR),
        _at("policies[0].kind", 4, "unknown kind 'mask'; write row_filter or column_mask"),
    ),
    (
        _policy("kind: row_filter, to: user:a, table: prod.t, expression: x"),
        _at(
            "policies[0].table",
            4,
            "'prod.t' is not ENV.NAMESPACE.TABLE; for example prod.gold.orders",
        ),
    ),
    (
        _grant("to: user:a, role: VIEWER, on: 5"),
        _at("grants[0].on", 4, "a target must be text, not 5; put it in quotes"),
    ),
    (
        _grant("to: user:a, role: VIEWER, table: 5"),
        _at("grants[0].table", 4, "a table must be text, not 5; put it in quotes"),
    ),
    (
        _INST + "groups: {g: {members: [1]}}\n",
        _at("groups.g.members[0]", 3, "a member must be text, not 1; put it in quotes"),
    ),
    (
        _INST + "service_accounts: [1]\n",
        _at(
            "service_accounts[0]", 3, "a service account name must be text, not 1; put it in quotes"
        ),
    ),
    (
        _ci(_CI + ", kinds: [5]"),
        _at("ci_trust[0].kinds[0]", 4, "unknown kind '5'; the kinds are deploy, dry_run"),
    ),
    (_PRE + "policies: [x]\n", _at("policies[0]", 3, "must be a mapping")),
    (_PRE + "ci_trust: [x]\n", _at("ci_trust[0]", 3, "must be a mapping")),
    (
        _ci("issuer: g, repository: r, as: user:a, on: prod, conditions: {ref: m}"),
        _at("ci_trust[0].as", 4, "a CI job acts as a service account; write service:NAME"),
    ),
    ("", "access.yaml: the file is empty; start with version: 1 and manages: [instance]"),
    ("- a\n", "access.yaml: must be a mapping; start with version: 1 and manages: [instance]"),
    ("manages: [prod]\n", "access.yaml: version: missing; write version: 1"),
    (
        "version: true\nmanages: [prod]\n",
        _at("version", 1, "version True is not supported; write version: 1"),
    ),
    (
        "version: 1\nmanages: prod\n",
        _at("manages", 2, "must be a list; write manages: [instance] or a list of environments"),
    ),
    (
        "version: 1\nmanages: []\n",
        _at("manages", 2, "is empty; write manages: [instance] or a list of environments"),
    ),
    (
        "version: 1\nmanages: [prod, prod]\n",
        _at("manages", 2, "lists a scope twice; remove the repeat"),
    ),
    (
        "version: 1\nmanages: [1]\n",
        _at("manages[0]", 2, "a scope must be text, not 1; put it in quotes"),
    ),
    ("version: 1\nmanages: [' p']\n", _at("manages[0]", 2, f"a scope ' p' {_STRICT}")),
    (
        _PRE + "groups: {g: {members: [a]}}\n",
        _at(
            "groups",
            3,
            "groups exist instance-wide; add instance to manages, or leave groups out and "
            "create groups with lakana group create",
        ),
    ),
    (
        _INST + "groups: [g]\n",
        _at("groups", 3, "must be a mapping; write finance: {members: [alice]}"),
    ),
    (
        _INST + "groups: {1: {}}\n",
        _at("groups.1", 3, "a group name must be text, not 1; put it in quotes"),
    ),
    (
        _INST + "groups: {g: [a]}\n",
        _at("groups.g", 3, "must be a mapping; write {members: [alice]}"),
    ),
    (
        _INST + "groups: {g: {members: a}}\n",
        _at("groups.g.members", 3, "must be a list; write [alice, bob]"),
    ),
    (
        _INST + "groups: {g: {members: [a, a]}}\n",
        _at("groups.g.members[1]", 3, "repeats groups.g.members[0]; remove one"),
    ),
    (
        _INST + "service_accounts: [a, a]\n",
        _at("service_accounts[1]", 3, "repeats service_accounts[0]; remove one"),
    ),
    (
        _INST + "service_accounts: a\n",
        _at("service_accounts", 3, "must be a list; write [etl-bot]"),
    ),
    (_INST + "grants: x\n", _at("grants", 3, "must be a list; write - {to: ..., ...}")),
    (_INST + "grants: [x]\n", _at("grants[0]", 3, "must be a mapping")),
    (
        _grant("role: VIEWER, on: prod"),
        _at("grants[0]", 4, "missing key 'to'; write to: group:NAME"),
    ),
    (
        _grant("to: 5, role: VIEWER, on: prod"),
        _at("grants[0].to", 4, "must be text such as user:alice, not 5"),
    ),
    (
        _grant("to: 'bot:a', role: VIEWER, on: prod"),
        _at("grants[0].to", 4, f"'bot:a' has the unknown kind 'bot'; {_SUBJECT_KINDS}"),
    ),
    (
        _grant("to: 'user:', role: VIEWER, on: prod"),
        _at("grants[0].to", 4, f"the name after user: '' {_STRICT}"),
    ),
    (
        _grant("to: user:a, role: 5, on: prod"),
        _at("grants[0].role", 4, "a role must be text, not 5; put it in quotes"),
    ),
    (_grant("to: user:a, role: VIEWER"), _at("grants[0]", 4, "give exactly one of on or table")),
    (
        _grant("to: user:a, role: VIEWER, on: prod, table: prod.g.t"),
        _at("grants[0]", 4, "give exactly one of on or table"),
    ),
    (
        _grant("to: user:a, role: VIEWER, on: .x"),
        _at(
            "grants[0].on",
            4,
            "'.x' is not a target: An environment name must not be empty. Write instance, "
            "an environment or ENV.NAMESPACE",
        ),
    ),
    (
        _policy("to: user:a, " + _EXPR),
        _at("policies[0]", 4, "missing key 'kind'; write kind: row_filter or kind: column_mask"),
    ),
    (
        _policy("kind: row_filter, to: user:a, table: prod.g.t, expression: ' '"),
        _at(
            "policies[0].expression",
            4,
            "an expression must be text that is not empty; put it in quotes",
        ),
    ),
    (
        _policy("kind: column_mask, to: user:a, " + _EXPR),
        _at("policies[0]", 4, "missing key 'column'; write column: email"),
    ),
    (
        _policy("kind: row_filter, to: user:a, column: c, " + _EXPR),
        _at(
            "policies[0].column", 4, "unknown key; the allowed keys are kind, table, expression, to"
        ),
    ),
    (
        _policy("kind: row_filter, to: user:a, table: staging.g.t, expression: x"),
        _at(
            "policies[0]",
            4,
            "staging is outside manages (prod); add staging to manages or remove the entry",
        ),
    ),
    (
        _policy("kind: row_filter, to: user:a, " + _EXPR)
        + "  - {kind: row_filter, to: user:a, "
        + _EXPR
        + "}\n",
        _at("policies[1]", 5, "repeats policies[0]; remove one"),
    ),
    (
        _ci("repository: r, as: service:a, on: prod, conditions: {ref: m}"),
        _at("ci_trust[0]", 4, "missing key 'issuer'; write issuer: github"),
    ),
    (
        _ci("issuer: g, repository: r, on: prod, conditions: {ref: m}"),
        _at("ci_trust[0]", 4, "missing key 'as'; write as: service:NAME"),
    ),
    (
        _ci("issuer: g, repository: r, as: service:a, on: prod, conditions: []"),
        _at("ci_trust[0].conditions", 4, "must be a mapping; write {ref: refs/heads/main}"),
    ),
    (
        _ci("issuer: g, repository: r, as: service:a, on: prod, conditions: {}"),
        _at(
            "ci_trust[0].conditions",
            4,
            "needs at least one condition, for example ref: refs/heads/main",
        ),
    ),
    (
        _ci("issuer: g, repository: r, as: service:a, on: prod, conditions: {rid: 7}"),
        _at("ci_trust[0].conditions.rid", 4, "a claim value must be text, not 7; put it in quotes"),
    ),
    (
        _ci(_CI + ", kinds: [rollback]"),
        _at("ci_trust[0].kinds[0]", 4, "unknown kind 'rollback'; the kinds are deploy, dry_run"),
    ),
    (
        _ci(_CI + ", kinds: []"),
        _at("ci_trust[0].kinds", 4, "needs at least one kind: deploy, dry_run or both"),
    ),
    (
        _ci(_CI + ", kinds: deploy"),
        _at("ci_trust[0].kinds", 4, "must be a list; write [deploy] or [deploy, dry_run]"),
    ),
    (_ci(_CI + ", enabled: 'no'"), _at("ci_trust[0].enabled", 4, "must be true or false")),
    (
        _ci(_CI.replace("on: prod", "on: staging")),
        _at(
            "ci_trust[0]",
            4,
            "staging is outside manages (prod); add staging to manages or remove the entry",
        ),
    ),
    (
        _ci(_CI) + "  - {" + _CI + ", kinds: [dry_run]}\n",
        _at("ci_trust[1]", 5, "repeats ci_trust[0]; remove one"),
    ),
]
_PREFIXED = [
    (
        _grant("to: user:a, action: FLY, on: prod"),
        "access.yaml: grants[0].action (line 4): unknown action 'FLY'; the actions are "
        "MANAGE_ENVIRONMENT, ",
    ),
    (
        _grant("to: user:a, role: VIEWER, on: 'p..x'"),
        "access.yaml: grants[0].on (line 4): 'p..x' is not a target: ",
    ),
    (
        _grant("to: user:a, role: VIEWER, table: 'p.g..t'"),
        "access.yaml: grants[0].table (line 4): 'p.g..t' is not a table: ",
    ),
]


@pytest.mark.parametrize(("text", "expected"), _CASES)
def test_more_problems_in_their_exact_words(text: str, expected: str) -> None:
    """Each branch of the reader says what is wrong where, in these words."""
    assert expected in _problems(text)


@pytest.mark.parametrize(("text", "start"), _PREFIXED)
def test_problems_that_quote_the_validator_start_alike(text: str, start: str) -> None:
    """The first words are fixed; the rest is the identifier validator's own sentence."""
    assert any(problem.startswith(start) for problem in _problems(text))


def test_every_problem_is_listed_at_once() -> None:
    """Three broken entries give three problems, not the first only."""
    text = (
        "version: 1\nmanages: [prod]\ngrants:\n"
        "  - {to: x, role: VIEWER, on: prod}\n"
        "  - {to: user:a, role: NOPE, on: prod}\n"
        "  - {to: user:a, role: VIEWER, on: elsewhere}\n"
    )
    assert len(_problems(text)) == 3


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nmanages: &m [prod]\nother: *m\n",
        "version: 1\nversion: 1\nmanages: [prod]\n",
        "version: 1\nmanages: [prod]\ngrants:\n  - {<<: {to: user:a}, role: VIEWER, on: prod}\n",
    ],
)
def test_anchors_aliases_merge_keys_and_duplicate_keys_are_refused(text: str) -> None:
    """The strict rules of project files."""
    assert _problems(text)[0].startswith("access.yaml")


def test_a_file_over_the_size_limit_is_refused_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size limit, named with the limit (patched small: the real one is 1 MiB)."""
    assert MAX_FILE_BYTES == 1024 * 1024
    monkeypatch.setattr(access_file, "MAX_FILE_BYTES", 100)
    problems = _problems("version: 1\nmanages: [prod]\n#" + "x" * 100)
    assert len(problems) == 1
    assert problems[0].startswith("access.yaml: the file is larger than 100 bytes; ")


def test_more_entries_than_the_limit_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry limit counts entries of every section, not YAML keys (patched small)."""
    assert MAX_ENTRIES == 10_000
    monkeypatch.setattr(access_file, "MAX_ENTRIES", 3)
    text = (
        "version: 1\nmanages: [instance]\ngroups: {g: {members: [a, b]}}\nservice_accounts: [s]\n"
    )
    assert _problems(text) == [
        "access.yaml: the file has 4 entries, more than 3; split access across fewer, broader "
        "grants (a group instead of many people)"
    ]


def test_a_file_with_far_too_many_keys_is_refused_by_the_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader's own cap on keys and list items turns into a problem too (patched small)."""
    monkeypatch.setattr(access_file, "_PARSE_LIMIT", 5)
    text = "version: 1\nmanages: [instance]\nservice_accounts: [a, b, c, d]\n"
    assert "more than 5 keys and list items" in _problems(text)[0]


def test_problems_beyond_fifty_are_counted_not_listed() -> None:
    """A file of broken entries does not make a message of megabytes."""
    rows = "".join("  - {to: x, role: VIEWER, on: prod}\n" for _ in range(60))
    problems = _problems(f"version: 1\nmanages: [prod]\ngrants:\n{rows}")
    assert len(problems) == 51
    assert problems[-1] == "access.yaml: and 10 more problems; fix these first, then check again"


def test_on_off_yes_no_stay_text() -> None:
    """YAML 1.2 booleans: `on:` is a key, `no` and `off` are names, `true` is still a boolean."""
    text = (
        "version: 1\nmanages: [instance]\n"
        "groups: {no: {members: [off, yes, 'on']}}\n"
        "grants:\n  - {to: group:no, role: VIEWER, on: instance}\n"
        "ci_trust:\n  - {issuer: g, repository: r, as: service:s, on: prod, "
        "conditions: {ref: main}, enabled: false, start_on_deploy_jobs: true}\n"
    )
    entries = dict((p, e) for p, e in parse_access_file(text, file_name="a.yaml").entries)
    assert entries["groups.no.members[0]"] == MembershipEntry("no", "off")
    assert entries["groups.no.members[1]"] == MembershipEntry("no", "yes")
    assert entries["grants[0]"] == GrantEntry(Subject("group", "no"), "VIEWER", None, Target())
    ci = entries["ci_trust[0]"]
    assert isinstance(ci, CiTrustEntry)
    assert (ci.enabled, ci.start_on_deploy_jobs) == (False, True)


def test_paths_and_lines_find_each_entry() -> None:
    """`path_of`, `line_of` and `state` answer for the entries the file holds."""
    parsed = parse_access_file(_FULL, file_name="access.yaml")
    grant = GrantEntry(Subject("group", "finance"), "ANALYST", None, Target("prod"))
    assert parsed.path_of(grant) == "grants[0]"
    assert parsed.path_of(GroupEntry("nope")) is None
    assert parsed.line_of("grants[0]") == 8
    assert parsed.line_of("grants[0].to") == 8
    assert parsed.line_of("groups.finance") == 4
    assert parsed.line_of("groups.finance.members[1]") == 4
    assert parsed.line_of("nothing") is None
    assert grant in parsed.state.entries


def test_the_state_splits_by_scope_and_type() -> None:
    """`owned`, `outside` and `of_type` on a parsed file."""
    state = parse_access_file(_FULL, file_name="access.yaml").state
    assert not state.outside(Scopes(frozenset({"instance"}))).entries
    prod = state.owned(Scopes(frozenset({"prod"})))
    assert all(not isinstance(e, GroupEntry) for e in prod.entries)
    assert len(state.outside(Scopes(frozenset({"prod"}))).of_type(GroupEntry)) == 2
    assert [g.name for g in state.of_type(GroupEntry)] == ["empty", "finance"]
    assert Scopes(frozenset({"a"})).union(Scopes(frozenset({"b"}))).names == {"a", "b"}


def test_scopes_own_instance_wide_entries_only_with_instance() -> None:
    """R3: groups and service accounts belong to `instance`; grants to their environment."""
    prod = Scopes(frozenset({"prod"}))
    grant = GrantEntry(Subject("user", "a"), "VIEWER", None, Target("prod"))
    assert prod.owns(grant)
    assert not prod.owns(GroupEntry("g"))
    assert not prod.owns(GrantEntry(Subject("user", "a"), "ADMIN", None, Target()))
    assert Scopes(frozenset({"instance"})).owns(GroupEntry("g"))


def test_the_fingerprint_ignores_order_and_changes_with_any_entry() -> None:
    """Same entries, same hash; one more entry, another hash."""
    a = GroupEntry("a")
    b = ServiceAccountEntry("b")
    assert fingerprint(AccessState(frozenset({a, b}))) == fingerprint(
        AccessState(frozenset({b, a}))
    )
    assert fingerprint(AccessState(frozenset({a}))) != fingerprint(AccessState(frozenset({a, b})))
    assert fingerprint(AccessState(frozenset({a}))) != fingerprint(
        AccessState(frozenset({a})), managed=[("instance", "0" * 64)]
    )


def test_describe_says_each_entry_in_one_line() -> None:
    """The plan's lines."""
    assert describe(GrantEntry(Subject("group", "f"), "ANALYST", None, Target("prod"))) == (
        "grant role ANALYST on prod to group:f"
    )
    assert describe(
        GrantEntry(Subject("user", "a"), None, "READ_TABLE", Target("prod", ("g",), "t"))
    ) == ("grant READ_TABLE on table prod.g.t to user:a")


@pytest.mark.parametrize("name", ["", "a/b", "..\\x", "a\nb", "x" * 256])
def test_a_file_name_is_a_plain_base_name(name: str) -> None:
    """The name the messages use cannot carry a path or a line break."""
    with pytest.raises(ValidationError):
        validate_file_name(name)


def test_a_target_knows_its_tier_its_namespace_and_its_text() -> None:
    """Instance, environment, namespace and table targets."""
    assert Target().kind == SecurableKind.INSTANCE
    assert str(Target()) == "instance"
    assert Target("prod").kind == SecurableKind.ENVIRONMENT
    assert Target("prod").namespace_joined is None
    namespace = Target("prod", ("gold", "sales"))
    assert (namespace.kind, str(namespace)) == (SecurableKind.NAMESPACE, "prod.gold.sales")
    assert namespace.namespace_joined == NamespacePath(("gold", "sales")).joined()
    table = Target("prod", ("gold",), "orders")
    assert (table.kind, str(table)) == (SecurableKind.TABLE, "prod.gold.orders")


def test_describe_and_entry_json_cover_every_entry_type() -> None:
    """The plan's lines, one per entry type, and the JSON each hashes."""
    ci = CiTrustEntry(
        "github",
        "acme-etl",
        "etl-bot",
        "prod",
        (("ref", "refs/heads/main"),),
        ("deploy", "dry_run"),
        enabled=False,
        start_on_deploy_jobs=True,
    )
    user = Subject("user", "alice")
    entries = [
        GroupEntry("finance"),
        MembershipEntry("finance", "alice"),
        ServiceAccountEntry("etl-bot"),
        GrantEntry(Subject("service", "etl-bot"), None, "WRITE_TABLE", Target("prod", ("g",), "o")),
        GrantEntry(user, None, "MANAGE_GRANT", Target()),
        RowFilterEntry(Subject("group", "eu"), Target("prod", ("g",), "o"), "region = 'EU'"),
        ColumnMaskEntry(user, Target("prod", ("g",), "c"), "email", "'***'"),
        ci,
    ]
    assert [describe(e) for e in entries] == [
        "group finance",
        "member alice of group finance",
        "service account etl-bot",
        "grant WRITE_TABLE on table prod.g.o to service:etl-bot",
        "grant MANAGE_GRANT on instance to user:alice",
        "row filter on prod.g.o for group:eu: region = 'EU'",
        "column mask on prod.g.c.email for user:alice: '***'",
        "CI trust github acme-etl → service:etl-bot on prod "
        "(ref=refs/heads/main; deploy+dry_run; disabled; starts on-deploy jobs)",
    ]
    assert entry_json(entries[4]) == {
        "type": "GrantEntry",
        "to": "user:alice",
        "role": None,
        "action": "MANAGE_GRANT",
        "on": "instance",
    }
    plain = CiTrustEntry("g", "r", "s", "prod", (("ref", "main"),))
    assert describe(plain) == "CI trust g r → service:s on prod (ref=main; deploy)"
    assert describe(GroupEntry("a\nb")) == "group a\\nb"
    assert [sort_key(e)[0] for e in entries] == [0, 1, 2, 3, 3, 4, 5, 6]


def test_a_valid_file_name_is_returned() -> None:
    """The base name passes through."""
    assert validate_file_name("access.yaml") == "access.yaml"
