# The access file

An access file lists, in one YAML file, who has access to what: groups and their members, service
accounts, grants, row filters, column masks and CI trust rules. You can write the file by hand or
start from `lakana access export`, keep it in git, and read what it would change with
`lakana access plan` before anything is applied. This page lists the exact format, the limits,
the two commands and every error. For the steps, see the how-to guides; for the lists and
explanations of access that exist today, see [Grants, permissions, principals and
groups](access.md).

This page covers reading a file: `export` and `plan` change nothing.

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
only in `conditions`, `kinds`, `enabled`, `start_on_deploy_jobs` or the service account, the plan
shows `~`. Every other difference is a removal and a creation.

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

## Limits

| Limit | Value |
|---|---|
| File size | 1 MiB (1048576 bytes). `lakana access plan` checks it before it sends the file; the server checks the request body against the same limit. |
| Entries | 10,000. |
| Problems listed at once | 50, then a count of the rest. |
| File name | The base name only, 1 to 255 characters, without `/` or `\`. |

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
| `! drift in prod: text` | Access in a managed scope that did not come from the file, with who made it, when and why when that is known. |
| `stops managing staging` | A scope the file no longer lists. |
| `No changes.` | The file and the live access agree. |
| `Fingerprint: ...` | A hash of the live access of the managed scopes, which an apply checks again. |

The problems of the file are not changes. When there are any, `plan` prints all of them on
stderr, each naming the file, the entry and its line, the cause and the next step, and exits with
code 1. A plan without problems exits with 0. `--output json` prints the plan as an object.

`--allow-lockout` accepts a file that drops your own `ADMIN` role while another active
administrator remains. A file that leaves no active administrator on the instance is always
refused.

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

## Who may use it

Both calls need the `ADMIN` role on the instance. They are refused with `403` for anyone else, and
the refusal is recorded in the audit log. They take a personal access token: a CI token or a task
token is refused, so a CI job cannot read or change who has access.

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
| `access.yaml: the file leaves no active ADMIN on the instance; keep {to: alice, role: ADMIN, on: instance} or give ADMIN to another active person` | Always refused. |
| `access.yaml: the file removes your own ADMIN role on the instance (MANAGE_GRANT); keep it, or repeat with --allow-lockout if another administrator applies from now on` | Pass `--allow-lockout` only when someone else will apply from now on. |
| `file_name must be 1 to 255 characters without / or \ or a control character; send the file's base name, such as access.yaml` | `422`: send the base name only. |
| `no environment X; lakana env list shows them` | `422`, for `export --scope`. |
