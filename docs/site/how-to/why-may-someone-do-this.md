# Why may (or may not) someone do this?

A person reports that a command is refused, or you want to know before you hand out a token what
a person will be able to do. `lakana access explain` answers both: for one principal and one
object, it lists each action, whether it is allowed, and the grant, role or group that allows it.
The answer comes from the same check the server makes when the person tries, so it cannot
disagree with what happens.

You need an administrator's profile (the `ADMIN` role on the instance) to explain someone else.
Anyone may explain themselves with their own profile. The steps below use the person `alice` with
the role `ANALYST` on the environment `dev`, as in
[Give a second person access](give-a-second-person-access.md).

## Ask about an environment

Name the principal and the object. `--scope dev` is the environment `dev`:

<!-- source: docs/examples/give-access/run.sh#explain -->
```bash
lakana access explain alice --scope dev
```

<!-- output: give-access#explain -->
```text
What alice may do on environment dev:
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ action             ┃ allowed ┃ via                             ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ READ_ENVIRONMENT   │ yes     │ role ANALYST on environment dev │
│ READ_NAMESPACE     │ yes     │ role ANALYST on environment dev │
│ SUBMIT_QUERY       │ yes     │ role ANALYST on environment dev │
│ MANAGE_ENVIRONMENT │ no      │                                 │
│ MANAGE_SECRET      │ no      │                                 │
│ MANAGE_CONNECTION  │ no      │                                 │
│ DEPLOY             │ no      │                                 │
│ RUN                │ no      │                                 │
└────────────────────┴─────────┴─────────────────────────────────┘
An action without a `via` is refused: no grant gives it here. Give one with `lakana grant create`, or look at the grants with `lakana grant list`.
```

Each row is one action that applies to an environment. `via` names what gives it:

- `role ANALYST on environment dev`: a role granted to the person, on this object or on one
  that contains it (a grant on an environment covers its namespaces and tables).
- `READ_TABLE granted on table silver.visits in environment dev`: a single action granted to
  the person.
- `group analysts: role VIEWER on environment dev`: either of the two, granted to a group the
  person is a member of.

When more than one grant gives an action, each is listed. A refused action has an empty `via`:
no grant gives it here.

## Ask about a namespace or a table

`--scope` takes the environment, then the namespace (dotted, as everywhere else), then the table,
separated by `/`: `dev/silver` is a namespace, `dev/silver/visits` a table. Without `--scope`
the answer is about the instance (managing principals, grants, repositories and the like).

A person explains themselves in the same way, with their own profile:

<!-- source: docs/examples/give-access/run.sh#explain-self -->
```bash
lakana access explain alice --scope dev/silver/visits
```

<!-- output: give-access#explain-self -->
```text
What alice may do on table silver.visits in environment dev:
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ action         ┃ allowed ┃ via                             ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ READ_TABLE     │ yes     │ role ANALYST on environment dev │
│ WRITE_TABLE    │ no      │                                 │
│ CREATE_TABLE   │ no      │                                 │
│ DROP_TABLE     │ no      │                                 │
│ MAINTAIN_TABLE │ no      │                                 │
└────────────────┴─────────┴─────────────────────────────────┘
An action without a `via` is refused: no grant gives it here. Give one with `lakana grant create`, or look at the grants with `lakana grant list`.
```

Asking about someone else without the `ADMIN` role on the instance is refused, whether that
principal exists or not, and the refusal is recorded in the audit log.

## In the web interface

A table's **Access** tab gives the same answer for you and that table: Read, Write, Drop and Edit
texts, each with the grant, role or group that allows it, or the reason it is refused. An
administrator also sees there who has access to the table, level by level, and its row filters
and column masks. The tab only shows; change the answer with the commands below. See
[Open a table](open-a-table.md#access).

## Change the answer

- **Allow an action:** grant a role or the action with `grant create`, on the object or on one
  that contains it ([Give a second person access](give-a-second-person-access.md#grant-the-smallest-set)
  shows how). The role `DATA_SCIENTIST` on the namespace `silver` would turn `WRITE_TABLE` and
  `CREATE_TABLE` to `yes` above. Then explain again.
- **Take an action away:** revoke the grant that `via` names with `grant revoke`. With
  `--output json` each entry of `via` carries the grant's id (`grant_id`); `grant list` with
  `--principal` or `--group` shows the same grants with their ids. A grant to a group gives the
  action to every member: remove the person from the group with `group remove-member` to take it
  from them alone.

## What the answer does not cover

- **A deactivated principal** can do nothing, because its tokens no longer work. The answer
  still shows what its grants would allow, and a line under the table says that it is
  deactivated.
- **A task's token** may do less than its principal: it reads and writes only what its task
  declares. The answer is about the principal's own grants.
- **`MANAGE_GRANT`, `MANAGE_GROUP` and `MANAGE_TOKEN`** work only with the `ADMIN` role on the
  instance. A grant of one of them as a single action shows as refused, with that reason in
  `--output json`.
- **Row filters and column masks** decide which rows and values a read returns, not whether
  the read is allowed. See [Show some people only some rows](filter-rows.md) and
  [Mask a column for some people](mask-a-column.md).

The options, the fields of the answer and the route are in
[Grants, permissions, principals and groups](../reference/access.md).
