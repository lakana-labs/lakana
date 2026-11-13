# Grants, permissions, principals and groups

The lists that show who has access, and the answer to "what may this principal do here, and
through which grant": who may read each, the filters, the order, the fields, the commands, the SDK
and the routes. For the steps, see [Give a second person access](../how-to/give-a-second-person-access.md)
and [Why may (or may not) someone do this?](../how-to/why-may-someone-do-this.md).

## Access in a file

To keep who has access in a file in git, write it with `lakana access export` and read what it would
change with `lakana access plan`. The format, the commands and the errors are on [The access
file](access-file.md). Once a file manages a scope, grants, groups, service accounts and policies
there are changed through the file: the commands that change them are refused with `409`, unless an
instance administrator overrides it ([Changes outside the file](access-file.md#changes-outside-the-file)).

## Who may read what

| What | Command | Route | Needs |
|---|---|---|---|
| Grants | `lakana grant list` | `GET /api/v1/grants` | the `ADMIN` role on the instance |
| A principal's permissions | `lakana access explain` | `GET /api/v1/principals/{name}/permissions` | nothing, about yourself; the `ADMIN` role on the instance, about anyone else |
| Principals | `lakana principal list` | `GET /api/v1/principals` | `MANAGE_PRINCIPAL` on the instance |
| Groups | `lakana group list` | `GET /api/v1/groups` | the `ADMIN` role on the instance |

A refusal of these reads is `403` and is recorded in the audit log.

## Grants

Without a filter the list has every active grant. Every filter narrows it; filters combine with
AND.

| Filter | Option and parameter | Values |
|---|---|---|
| Principal | `--principal`, `principal_name` | the grants made to this principal; not those it has through a group (`access explain` shows those) |
| Group | `--group`, `group_name` | the grants made to this group; at most one of principal and group |
| Action | `--action`, `action` | the grants that give this action, in any letter case: a grant of the action itself, or of a role that carries it |
| Object | `--env`, `--ns`, `--table`; `environment`, `namespace`, `table` | the grants on exactly this object: a table (all three), a namespace (environment and namespace) or an environment (environment alone). Grants on an object that contains it are not listed |

The order is the time a grant was made, newest first; equal times by id. `--limit` and `--cursor`
page through the list as they do for the [runs, queries and deploys lists](lists.md#paging): 1 to
200 grants per page, 100 by default, and the cursor of the next page in the `X-Next-Cursor`
header. The cursor of one list is refused by every other list.

## Permissions

The answer for one principal and one object, one entry per action that applies to an object of
that kind. `allowed` is the answer of the check the server makes when the principal tries, so it
cannot differ from what a route decides.

| Object | `--scope` | Parameters | Actions |
|---|---|---|---|
| The instance | left out | none | `MANAGE_ENVIRONMENT` (creating one), `MANAGE_PRINCIPAL`, `MANAGE_GROUP`, `MANAGE_TOKEN`, `MINT_TASK_TOKEN`, `MANAGE_GRANT`, `MANAGE_POLICY`, `READ_AUDIT_LOG`, `READ_QUERY_TEXT`, `MANAGE_REPOSITORY`, `MANAGE_PACKAGE_INDEX` |
| An environment | `dev` | `environment` | `READ_ENVIRONMENT`, `READ_NAMESPACE`, `SUBMIT_QUERY`, `MANAGE_ENVIRONMENT`, `MANAGE_SECRET`, `MANAGE_CONNECTION`, `DEPLOY`, `RUN` |
| A namespace | `dev/gold.sales` | `environment`, `namespace` | `READ_NAMESPACE`, `CREATE_NAMESPACE`, `MANAGE_NAMESPACE`, `DROP_NAMESPACE`, `CREATE_TABLE` |
| A table | `dev/gold.sales/orders` | `environment`, `namespace`, `table` | `READ_TABLE`, `WRITE_TABLE`, `CREATE_TABLE`, `DROP_TABLE`, `MAINTAIN_TABLE` |

The namespace is dotted, as everywhere else. A namespace needs its environment and a table its
namespace (`422` otherwise).

The fields of the answer (`--output json` prints them as they are):

| Field | Meaning |
|---|---|
| `principal` | the principal's name |
| `active` | whether it may sign in |
| `scope` | the object: `kind` (`INSTANCE`, `ENVIRONMENT`, `NAMESPACE`, `TABLE`), `environment`, `namespace`, `table` |
| `permissions` | one entry per action, in the order of the table above |
| `permissions[].action` | the action |
| `permissions[].allowed` | `true` or `false` |
| `permissions[].via` | the grants that give it, oldest first; empty when refused |
| `permissions[].via[].kind` | `role` (a role granted to the principal), `grant` (the action granted to it alone) or `group` (either, granted to a group it is a member of) |
| `permissions[].via[].grant_id` | the grant's id, as `lakana grant revoke` takes it |
| `permissions[].via[].group` | the group's name, for `group` |
| `permissions[].via[].role` | the role, or `null` for a single action |
| `permissions[].via[].scope` | what the grant is on: the object asked about, or one that contains it |
| `permissions[].reason` | why it is refused, or `null` |
| `note` | for a deactivated principal: its tokens no longer work, so it can do none of this; the answers show what its grants would allow |

What the answer takes into account:

- **Grants on a containing object.** A grant on the instance covers everything; one on an
  environment covers its namespaces and tables; one on a namespace covers the namespaces inside
  it and their tables.
- **`MANAGE_GRANT`, `MANAGE_GROUP` and `MANAGE_TOKEN`** need the `ADMIN` role on the instance.
  A grant of one of them as a single action has no effect, so the action shows as refused with
  the reason `... needs the ADMIN role on the instance`, and `via` names only the role.
- **`READ_QUERY_TEXT`** is in no role, `ADMIN` included, so an administrator sees it refused
  until it is granted as a single action on the instance
  (`lakana grant create --kind INSTANCE --principal NAME --action READ_QUERY_TEXT`). It covers
  query text and column values in other people's audit events (the SQL text of a query, the
  watermark of an incremental write), which otherwise show as a SHA-256 and length (query
  text) or as `{"hidden": true}` (a watermark)
  ([Other people's query text and column values](lists.md#other-peoples-query-text-and-column-values-in-the-audit-log)).
- **`MANAGE_CONNECTION`** creates, changes and deletes the connections of an environment (the
  source systems its assets read); reading them needs only `READ_ENVIRONMENT`. The roles
  `DATA_ENGINEER` and `ADMIN` carry it. It may give a connection a new secret or keep the one
  bound to it; naming any other secret also needs `MANAGE_SECRET`, which only `ADMIN` carries. So
  does changing, on a connection that has a secret, its address or how the secret is sent (TLS
  weakened, CA certificate, `auth`, `api_key_in`), unless the same change gives a new secret.
  A task's token never has it.
- **`MINT_TASK_TOKEN`** is the right to mint a task token for oneself. Minting one for another
  principal needs the `ADMIN` role on the instance too.
- **Task tokens** may not ask, not even about their own principal: a task's token can do less
  than its principal, and the answer is about the principal's grants.
- **An object that does not exist** is `404` for an administrator. A principal asking about
  itself gets the answer for an object that no grant names, so the answer never tells it whether
  something exists. A principal that does not exist is `404` for an administrator and `403` for
  everyone else, the same answer as for one that exists.

## Principals and groups

`--search` (`q`) lists the principals or groups whose name contains the text, in any letter case;
`%` and `_` match only themselves. A blank search lists all. The order is the time a principal
or group was created, newest first; equal times by id. `--limit` and `--cursor` page as for the
grants.

## Refusals

| Cause | Answer |
|---|---|
| `principal_name` and `group_name` together | `422`; a grant is made to one or the other |
| a namespace without its environment, a table without its namespace | `422`; the message names what is missing |
| an unknown action | `422`; the message lists the actions |
| `READ_QUERY_TEXT` granted on an environment, a namespace or a table | `422`; it is given on the instance only, and the message shows the command |
| a `limit` outside 1 to 200, a cursor that was not issued or came from another list | `422` |
| a caller without the right in "Who may read what" | `403`, audited |
| an unknown principal, group, environment, namespace or table, for an administrator | `404` |
| an unknown principal, for anyone else | `403`, audited |
| a change in a scope an access file manages | `409`; the message names the scope and the next step, and `--override-managed --reason` overrides it ([The access file](access-file.md#the-override)) |

## SDK and REST

| What | SDK | Route |
|---|---|---|
| Grants | `client.grants.list(...)`; `client.grants.list_page(...)` also returns the cursor; `client.grants.iter(...)` every grant | `GET /api/v1/grants?principal_name=&group_name=&action=&environment=&namespace=&table=&limit=&cursor=` |
| Permissions | `client.principals.permissions(name, environment=, namespace=, table=)` | `GET /api/v1/principals/{name}/permissions?environment=&namespace=&table=` |
| Principals | `client.principals.list(search=)`, `list_page`, `iter` | `GET /api/v1/principals?q=&limit=&cursor=` |
| Groups | `client.groups.list(search=)`, `list_page`, `iter` | `GET /api/v1/groups?q=&limit=&cursor=` |

The lists answer with a JSON list and put the cursor of the next page in the `X-Next-Cursor`
header (absent on the last page). The permissions route answers with one object, the fields above.

```python
import lakana

client = lakana.Client()
answer = client.principals.permissions("alice", environment="dev", namespace="silver")
for permission in answer.permissions:
    print(permission.action, permission.allowed, [via.grant_id for via in permission.via])
for grant in client.grants.iter(action="READ_TABLE"):
    who = grant.principal.name if grant.principal else grant.group
    print(grant.id, who, grant.role or grant.action)
```

A grant, a row filter and a column mask carry the name of who they are for. `principal` is
`{id, name, principal_type}` (`principal_type` is `user` or `service`) for a principal and `null`
for a group; `group` is the group's name and `null` for a principal. The ids `principal_id` and
`group_id` stay. The names are read in one query for the whole answer, and only a caller who may
list the grants (an administrator) or the row filters and column masks (`MANAGE_POLICY`) sees
them. A person or group deleted since answers `null`. A server before this release sends none:
the SDK leaves both `None`.
