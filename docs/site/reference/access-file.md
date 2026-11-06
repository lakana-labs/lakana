# The access file

An access file lists, in one YAML file, who has access to what: groups and their members, service
accounts, grants, row filters, column masks and CI trust rules. You can write the file by hand or
start from `lakana access export`, keep it in git, read what it would change with
`lakana access plan`, and make the changes with `lakana access apply`. This page lists the exact
format, the limits, the four commands and every error. For the steps, see [Keep access in a
reviewed file](../how-to/keep-access-in-a-reviewed-file.md); for the lists and
explanations of access that exist today, see [Grants, permissions, principals and
groups](access.md).

`export`, `plan` and `status` change nothing; `apply` makes the changes, and from then on a scope a
file manages refuses changes made outside it.

## An example

```yaml
version: 1
manages: [instance]

groups:
  finance:
    members: [alice, bob]
  engineers:
    members: [carol]

service_accounts:
  - etl-bot

grants:
  - {to: group:engineers, role: ADMIN, on: instance}
  - {to: group:finance, role: ANALYST, on: prod}
  - {to: group:finance, role: VIEWER, on: prod.gold.sales}
  - {to: service:etl-bot, role: DATA_ENGINEER, on: prod}
  - {to: user:alice, action: READ_TABLE, table: prod.gold.sales.orders}

policies:
  - kind: row_filter
    table: prod.gold.sales.orders
    expression: "region = 'EU'"
    to: group:finance
  - kind: column_mask
    table: prod.gold.sales.orders
    column: email
    expression: "'***'"
    to: group:finance

ci_trust:
  - issuer: github
    repository: analytics
    as: service:etl-bot
    on: prod
    conditions:
      repository_id: "123456789"
      ref: refs/heads/main
```

`on:` needs no quotes: in an access file only `true` and `false` are booleans, so `on`, `no`,
`off` and `yes` are text.

## The format

The file is one YAML mapping. Every key below is checked: an unknown key is an error that names
the allowed ones. Only plain YAML is accepted: no anchors, no aliases, no merge keys (`<<`), no
tags and no duplicate keys, because each would hide from a reviewer what the file grants.

| Key | Type | Meaning |
|---|---|---|
| `version` | the number `1` | Required. |
| `manages` | list of scopes | Required. `[instance]`, or a list of environment names. The scopes the file owns, see [What `manages` owns](#what-manages-owns). |
| `groups` | mapping | Optional, and only with `manages: [instance]`. Group name to `{members: [names]}`. A member is a user's name. |
| `service_accounts` | list of names | Optional, and only with `manages: [instance]`. The service accounts the instance has. |
| `grants` | list | Optional. See [Grants](#grants). |
| `policies` | list | Optional. Row filters and column masks, see [Policies](#policies). |
| `ci_trust` | list | Optional. CI trust rules, see [CI trust rules](#ci-trust-rules). |

A name is text of 1 to 255 printable characters without blanks at either end. Put a name that
YAML would read as a number or a date in quotes.

### Subjects

A grant or a policy says who it is for with `to:`, written `kind:name`:

| Kind | Meaning |
|---|---|
| `user:alice` | a person |
| `group:finance` | a group; with `manages: [instance]` it must be listed under `groups`, otherwise it must exist |
| `service:etl-bot` | a service account |

### Grants

| Key | Meaning |
|---|---|
| `to` | Required. A subject. |
| `role` or `action` | Exactly one. A role (`VIEWER`, `ANALYST`, `DATA_SCIENTIST`, `DATA_ENGINEER`, `ADMIN`) or a single action such as `READ_TABLE`. [Grants, permissions, principals and groups](access.md) lists the actions. |
| `on` or `table` | Exactly one. `on: instance`, `on: prod` (an environment) or `on: prod.gold.sales` (a namespace); `table: prod.gold.sales.orders` (environment, namespace, table). |

### Policies

Every policy has `kind`, `table`, `expression` and `to`; a column mask also has `column`.

| Key | Meaning |
|---|---|
| `kind` | `row_filter` or `column_mask`. |
| `table` | `ENV.NAMESPACE.TABLE`, as in `prod.gold.sales.orders`. |
| `column` | Column masks only: the column the mask applies to. |
| `expression` | The SQL expression. The filter keeps the rows for which it is true; the mask replaces the column's value with it. Put it in quotes. |
| `to` | One subject. |

A policy follows its table's columns: after a column is renamed, a file that still uses the old
name matches the same policy, and `export` writes the current name.

### CI trust rules

A rule says which CI jobs may deploy which repository, see [CI trust rules and CI
tokens](ci-trust-rules.md).

| Key | Default | Meaning |
|---|---|---|
| `issuer` | required | `github`, `gitlab` or the URL of a configured issuer. |
| `repository` | required | The registered Lakana repository the job may deploy. |
| `as` | required | The service account the job acts as, written `service:NAME`. |
| `on` | required | One environment, such as `prod`. |
| `conditions` | required | Claim name to the exact text the ID token's claim must have, for example `ref: refs/heads/main`. At least one; the server also requires the numeric repository id (`repository_id` on GitHub, `project_id` on GitLab) and a `ref` or `environment`. |
| `kinds` | `[deploy]` | `deploy`, `dry_run` or both. |
| `enabled` | `true` | `false` keeps the rule but switches it off. |
| `start_on_deploy_jobs` | `false` | Let a deploy under the rule start the project's `on_deploy` jobs. |

A rule is the one entry that changes in place: if the file's rule differs from the live one
only in `kinds`, `enabled` or `start_on_deploy_jobs`, the plan shows `~`. A difference in the
issuer, the repository, the service account (`as`), the environment (`on`) or the `conditions`
makes a different rule: the plan shows a removal and a creation.

## What `manages` owns

`manages` is the list of scopes the file owns completely. For those scopes the file is the whole
truth: access that exists there and that the file does not list is **removed** by an apply, and
`plan` shows each such removal with a `-`.

| `manages` | Owned |
|---|---|
| `[instance]` | everything on the instance: every group, membership, service account, grant, policy and CI trust rule |
| `[prod, staging]` | grants on those environments, on anything inside them, and the policies and CI trust rules of their tables and environments. Groups, memberships and service accounts are not covered. |

An entry outside `manages` is an error. `instance` already covers every environment, so the two
are not mixed. There is one access file per instance: two files that both manage the same scope
would each undo the other.

## Changes outside the file

Once an apply has made a scope managed, the file is the only way to change access in it. Every
command, SDK method and route that would change access there is refused with `409`:

`` `prod` is managed by an access file (last applied 2026-11-06T09:30Z); change the file and run `lakana access apply`, or, in an emergency, repeat with `--override-managed --reason …` ``

The time is the last apply of that scope, in UTC. An environment is managed when the file lists
it, or when the file lists `instance`, which covers every environment. Groups, memberships,
service accounts and grants on the instance are guarded only when `instance` is managed.

| Guarded | Commands |
|---|---|
| Grants | `lakana grant create`, `lakana grant revoke` |
| Groups and their members | `lakana group create`, `delete`, `add-member`, `remove-member` |
| Service accounts | `lakana principal create --type service-account`, and `lakana principal deactivate` of a service account |
| Row filters and column masks | `lakana row-filter create`, `revoke`; `lakana column-mask create`, `revoke` |
| CI trust rules | `lakana ci-trust create`, `update`, `enable`, `disable`, `delete` |

Not guarded, because a file does not hold them: people (user principals) and personal access
tokens. An override on the commands for people is refused (`people are not managed by an access
file; repeat without --override-managed`). Dropping a table or an environment is not guarded
either: it takes the grants and policies of the object with it. Restoring one
(`lakana table undrop`, `lakana env restore`) is not guarded, so that a recovery never waits
for the file, and it re-adds the grants and policies the object had. In a managed scope the next
plan lists them as removals (`-`), and the next apply removes them unless the file lists them. A
call that changes nothing, such as revoking a grant that is already revoked, is not refused.

### The override

In an emergency an instance administrator can make the change anyway, by adding
`--override-managed --reason "Incident 42: bob needs to read prod to debug the outage"` to the
command. The CLI warns on stderr: `Overriding a scope an access file manages; the next apply
reverts this unless the file lists it.` The override:

- needs the `ADMIN` role on the instance, even where the change itself needs less (a row filter
  needs only `MANAGE_POLICY`). Anyone else is refused with `403` (`Only an instance administrator
  may override a scope an access file manages; ask one, or change the file`), and the refusal is
  recorded in the audit log;
- needs a reason of 10 to 500 characters on one line;
- is recorded in the audit log as `MANAGED_SCOPE_OVERRIDDEN`, with the `scope`, the `change` and
  the `reason`, in the same transaction as the change;
- is listed by every plan as drift, for example `! drift in prod: grant VIEWER to bob (override)`
  followed by who, when and why, until the next apply. That apply reverts the change unless the
  file now lists it.

`--override-managed` without `--reason`, or `--reason` without `--override-managed`, is refused
before anything is sent. In a scope that no file manages the override is refused too
(`` `staging` is not managed by an access file; repeat without `--override-managed` ``), so a
script that still carries the flag does not hide its changes.

Over REST the override is two query parameters of the guarded route,
`?override_managed=true&override_reason=...`; one without the other is refused with `422`. In the
SDK, each method that changes access takes `override_reason="..."`.

When no active administrator is left, the server restores its bootstrap administrator when it
starts (`LAKANA_BOOTSTRAP_ADMIN` in [Settings](settings.md)). That grant is the one change the
guard lets through, so recovering from a lockout never depends on the file. It is recorded like
an override, with the reason `Lockout recovery at server start: no active instance
administrator`, and the next plan lists it as drift.

## Limits

| Limit | Value |
|---|---|
| File size | 1 MiB (1048576 bytes). `lakana access plan` checks it before it sends the file, and checks the request it would send (the text escaped as JSON, plus the file name); the server checks the request body against the same limit. |
| Entries | 10,000. |
| Problems listed at once | 50, then a count of the rest. |
| File name | The base name only, 1 to 255 characters, without `/` or `\`. |

An export of a large instance can exceed the size or entry limit. Export and keep one file per
environment instead: `lakana access export --scope prod -o prod.yaml`.

## Environment names

The file writes a target as `instance`, an environment, or `ENV.NAMESPACE`. An environment
therefore cannot be called `instance` (in any case) or contain a `.`, and `lakana env create`
refuses such names, as it does a name longer than 255 characters or with blanks at either end.
An environment made by an older release with such a name cannot be exported or planned: `export`
and `plan` name it and ask you to create the environment again under another name (`prod-eu`
instead of `prod.eu`) and move its access there. Other environments are not affected; export them
one by one with `--scope`.

A deactivated person or service account is left out of an export. A service account that is
deactivated cannot be used again: its name stays taken.

## Export

`lakana access export` writes the instance; `lakana access export --scope prod --scope staging
-o access.yaml` writes two environments to a file.

`--scope` is `instance` (the default) or an environment, and can be repeated for environments.
Without `-o` the file is printed. The file starts with a comment that says how to review and
plan it. Planning an export that you did not edit plans nothing: that is the starting point for
a file you keep in git.

Route: `GET /api/v1/access/export?scope=prod&scope=staging`, answering `{"text": "..."}`. SDK:
`client.access.export(["prod", "staging"])`.

## Plan

`lakana access plan access.yaml` reads the file and prints the plan.

`plan` sends the file to the server, which reads it and compares it with the live access. It
changes nothing. It prints:

| Line | Meaning |
|---|---|
| `+ text  (grants[3])` | An entry the apply would create, and where the file lists it. |
| `~ text  (ci_trust[0])` | A CI trust rule the apply would change in place; the text shows the rule before and after. |
| `- text` | Access that exists in a managed scope and that the file does not list; the apply would remove it. |
| `! drift in prod: text` | Access in a scope that no earlier apply of a file has managed yet, made outside any file; the apply would remove it, so look before you apply. The scope is the environment, or `instance`. When known, `— by, at: reason` follows: who made it, when (a timestamp) and why. These are printed only when known; a first apply has none. In a managed scope, drift is an override, and its text ends with `(override)` (see [The override](#the-override)). |
| `starts managing prod` | The scope is not managed yet; applying the file starts managing it, and changes made by hand there are then refused (see [Changes outside the file](#changes-outside-the-file)). Not printed for an environment that a managed `instance` already covers. |
| `stops managing prod` | An earlier apply managed the scope and the file no longer lists it; the apply releases it. |
| `No changes.` | The file and the live access agree and the apply starts and stops managing no scope. |
| `Fingerprint: ...` | A hash of the exact text of the file and of the live access of the managed scopes. An apply plans the file again and checks it: a different file, or access changed since, gives a different value. |

The problems of the file are not changes. When there are any, `plan` prints all of them on
stderr, each naming the file, the entry and its line, the cause and the next step, and exits with
code 1. A plan without problems exits with 0. `--output json` prints the plan as an object.

`--allow-lockout` accepts a file that drops your own `ADMIN` role while another active
administrator remains. That administrator must be an existing active user or service account,
or a member of a group the file gives `ADMIN`: a service account the same file creates does not
count, since it has no token yet. A file that leaves no active administrator on the instance is
always refused.

Route: `POST /api/v1/access/plan` with `{"file_name": "access.yaml", "text": "...",
"allow_lockout": false}`. A file with problems is a plan with problems, answered with `200`.

| Field | Meaning |
|---|---|
| `file_name`, `file_sha256` | The base name and the hash of the text. |
| `manages` | The scopes the file manages; empty when it did not parse. |
| `creates`, `changed`, `removals` | Lists of `{op, entry, text}`. `changed` holds the in-place changes of CI trust rules only. `entry` is `null` for a removal. |
| `releases` | Scopes the file no longer manages. |
| `drift` | Lists of `{scope, text, by, reason, at}`. |
| `problems` | One text per problem. |
| `fingerprint` | The hash described above. |

SDK: `client.access.plan(text, file_name="access.yaml", allow_lockout=False)` returns an
`AccessPlan` with the same fields.

## Apply

`lakana access apply access.yaml` makes the file's changes in one transaction: all of them or
none, with one `ACCESS_APPLIED` event in the audit log and every change linked to it by an apply
id. It records the scopes the file manages, the file's hash and the commit, and from then on
those scopes refuse changes made outside the file (see [Changes outside the
file](#changes-outside-the-file)). A scope that an earlier apply managed and the file no longer
lists is released: the plan shows `stops managing prod`, and the apply prints `Stopped managing:
prod`. An environment that a managed `instance` still covers is not released, and the plan lists every open override the apply resolves, in the scopes managed before or after.

| Option | Meaning |
|---|---|
| `--plan-fingerprint F` | The `Fingerprint:` of the plan you reviewed. The command then applies without asking, even without `--yes`, since that plan was the review; it is refused with `409` when the file or the access changed since: the apply does what the plan showed, for exactly that file text. |
| `--source-commit SHA` | The commit the file came from (7 to 64 hex digits). Recorded with the apply and shown by `status`. |
| `--yes` | Do not ask. |
| `--allow-lockout` | As for `plan`. |

Without `--plan-fingerprint` the command plans the file, prints the plan, asks `Apply these
changes?` (unless `--yes`; it asks only when it plans itself), and applies exactly that plan. Without `--yes` and without a
terminal it stops before anything changes, with a message that names `--yes`: in CI pass `--yes`,
and `--plan-fingerprint` to apply the plan an earlier step showed. A plan with problems is never
applied. The server makes the plan again under a lock and compares its fingerprint, so what you
reviewed is what is applied. The command prints `Applied: 3 created, 0 changed, 1 removed.`;
`--output json` prints the result as an object. The SDK never repeats an apply request on its
own.

Route: `POST /api/v1/access/apply` with `{"file_name": "access.yaml", "text": "...",
"plan_fingerprint": "64 hex digits", "source_commit": null, "allow_lockout": false}`, answering
`200` with `apply_id`, `file_sha256`, `source_commit`, `created`, `changed`, `removed`,
`released` and `applied_at`. SDK: `client.access.apply(text, plan_fingerprint=..., file_name=...,
source_commit=..., allow_lockout=False)` returns an `AccessApplyResult`.

## Status

`lakana access status` lists each managed scope with the file, the commit, who applied it and
when. Under `Open overrides (the next apply reverts them unless the file lists them):` it lists
the changes made with `--override-managed` since the scope's last apply, with who, when and why.
`--output json` prints both lists. Route: `GET /api/v1/access/status`, answering `{"scopes":
[...], "overrides": [...]}`. SDK: `client.access.status()` returns an `AccessStatus`.

## Who may use it

All four commands need the `ADMIN` role on the instance. They are refused with `403` for anyone
else, and the refusal is recorded in the audit log. They take a personal access token: a CI token
or a task token is refused with `403` on all four routes, so a CI job cannot read or change who has
access. A CI job that applies a file therefore holds an administrator's personal token as a
secret; letting a CI token apply is a later option, tracked in
[issue 455](https://github.com/lakana-labs/lakana/issues/455). The how-to describes the stricter
way, where CI only plans.

## Errors

| Message | Cause and next step |
|---|---|
| `access.yaml: the file is empty; start with version: 1 and manages: [instance]` | Write the two required keys. |
| `access.yaml: version: missing; write version: 1` | Add `version: 1`. |
| `access.yaml: manages: missing; write manages: [instance] or a list of environments` | Add `manages`. |
| `access.yaml: manages (line 2): no environment prod; create it with lakana env create, or remove it from manages` | The environment does not exist. |
| `access.yaml: x (line 3): unknown key; the allowed keys are ...` | A misspelt or unsupported key; the message lists the allowed ones. |
| `access.yaml: grants[3] (line 20): give exactly one of role or action` | Remove one of the two. Likewise `give exactly one of on or table`. |
| `... unknown role 'X'; the roles are ...`, `... unknown action 'X'; the actions are ...` | Use one of the listed names. |
| `... 'alice' names no kind; write user:alice, group:alice or service:alice` | Write the subject with its kind. |
| `... prod is outside manages (instance); add prod to manages or remove the entry` | The entry lies in a scope the file does not own. |
| `... groups exist instance-wide; add instance to manages, or leave groups out and create groups with lakana group create` | Groups, memberships and service accounts need `manages: [instance]`. |
| `... no group finance in the file; list it under groups` | With `manages: [instance]`, a group named by a grant must be listed. |
| `... no active user alice; ...` | The person does not exist or is deactivated. |
| `... alice is a person; write user:alice` | The kind does not match the name. |
| `... repeats groups.finance; remove one` | The same entry is listed twice. |
| `access.yaml: the file is larger than 1048576 bytes; use a group instead of many people, or remove entries that no longer apply` | See [Limits](#limits). |
| `access.yaml: the file has 10001 entries, more than 10000; split access across fewer, broader grants` | See [Limits](#limits). |
| `access.yaml: the file leaves no active ADMIN on the instance; keep {to: alice, role: ADMIN, on: instance} or give ADMIN to another active user or service account` | Always refused. |
| `access.yaml: the file removes your own ADMIN role on the instance (MANAGE_GRANT); keep it, or repeat with --allow-lockout if another administrator applies from now on` | Pass `--allow-lockout` only when someone else will apply from now on. |
| `file_name must be 1 to 255 characters without / or \ or a control character; send the file's base name, such as access.yaml` | `422`: send the base name only. |
| `no environment X; lakana env list shows them` | `422`, for `export --scope`. |
| `the file or the access changed since your plan (fingerprint 3fa9c1…); nothing was applied. Run lakana access plan again and review the changes` | `409`. The file or the access changed after your plan (a different file text, or someone changed access). Plan again, read the new plan, and apply with its fingerprint. |
| `access.yaml: ...` (one line per problem, the same texts as above) | `422` from `apply`: the file has problems and nothing was applied. All are listed, at most 50 and then `and N more problems`. Fix them, then plan again. |
| `the file removes your own ADMIN role in more than one change, so it would end before the rest of the changes can be made; nothing was applied. Apply in two steps: ...` | `409`. Your last way to administer is a group whose `ADMIN` grant and whose membership of you both go. Apply a first file that keeps one of them, then the file without it. |
| `access.yaml: service_accounts: the file removes service:access-admin, the account applying it, which would end its access part-way through the apply; have another administrator apply this file` | A problem in the plan, so `422` on apply. The account applying the file cannot remove itself. Another administrator applies the file, or keep the account in `service_accounts`. |
| `` `prod` is managed by an access file (last applied ...); change the file and run `lakana access apply`, or, in an emergency, repeat with `--override-managed --reason …` `` | `409`, from a command that changes access by hand. See [Changes outside the file](#changes-outside-the-file) and [The override](#the-override). |
| `lakana access apply asks before it changes access; in CI pass --yes, with --plan-fingerprint from the plan step` | The CLI, exit 1: there is no terminal to ask on. Pass `--yes`. |
