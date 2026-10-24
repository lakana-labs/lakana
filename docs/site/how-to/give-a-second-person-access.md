# Give a second person access

After this guide a second person can sign in to your instance and query one environment, and
can do nothing else. You also know how to see who has access and how to take it away. You need an
instance that is running, and an administrator's profile: creating principals, grants and tokens
needs the `ADMIN` role on the instance. In local mode and in the Compose bundle the first profile
is one (see [Getting started](../tutorials/getting-started.md) and
[Deploying with Compose](deploy-with-compose.md)). The person you give access to needs the `lakana`
command on their machine and a network route to the instance. The web interface is not part of
this release, so a **token** is the way in for now: a secret string that stands for one person.

The steps below give a person called `alice` access to the environment `dev`. Use your own names.

## Create the person

A person is a principal of the type `user`:

<!-- source: docs/examples/give-access/run.sh#create -->
```bash
lakana principal create alice --type user
```

<!-- output: give-access#create -->
```text
Created user alice
It can do nothing yet. Next:
  lakana grant create --principal alice --role ANALYST --kind ENVIRONMENT --env ENV
  lakana token issue alice NAME
```

A principal can do nothing until it has a grant. The command prints the two steps that follow.

## Grant the smallest set

The role `ANALYST` on one environment lets a person read every table in it and run queries. It
does not let them create tables, and it carries none of the rights to deploy, to run or to
change who may do what:

<!-- source: docs/examples/give-access/run.sh#grant -->
```bash
lakana grant create --principal alice --role ANALYST --kind ENVIRONMENT --env dev
```

<!-- output: give-access#grant -->
```text
Created grant c913f5e7-8bf0-4b21-9630-46de66a0082d (ANALYST on environment dev) to alice
```

The role `VIEWER` is smaller still: it reads tables and cannot run a query. Roles that do more
(`DATA_SCIENTIST`, `DATA_ENGINEER`, `ADMIN`) are for people who deploy or administer.

!!! note "Hide rows or columns before you give access"
    A grant on an environment covers every table in it, tables that are created later included.
    If some people must not see some rows or some values, create the
    [row filter](filter-rows.md) or the [column mask](mask-a-column.md) first, then the grant.

## Issue a token

A token belongs to one principal and carries the principal's access. Name it after the place it
will be used, so that you can tell tokens apart when you revoke one:

<!-- source: docs/examples/give-access/run.sh#token -->
```bash
lakana token issue alice alice-laptop
```

The secret is printed once and cannot be shown again; the two notices go to the error output: one
says so, one says whom to give the token to and what they type. Here the secret is shortened:

<!-- output -->
```text
Issued token 634fb88f45cabc42 (alice-laptop) for alice
Token: lakana_pat_634fb88f45cabc42_<secret>
Store this token now: it can't be shown again.
Give this token to alice over a trusted channel; they run `lakana login`.
```

Hand over three things: the token, the address of the instance (the URL that the person passes to
`lakana login`, such as `https://lakana.example.org`), and the name of the environment. Send the
token through a channel that you trust, such as the sharing function of a password manager or a
message that disappears, and not by e-mail or in a ticket. The address and the environment are not
secret. Never put the token in a file of a git repository.

## What the new person types

The person signs in once. `lakana login` saves the address and the token in a profile on their
machine and uses them from then on. With `--token-stdin` it reads the token from its input, which
suits a script; without that option it asks for the token and hides what is typed.

<!-- source: docs/examples/give-access/run.sh#login -->
```bash
printf '%s\n' "$TOKEN" | lakana login --url "$URL" --env dev --token-stdin
```

`$URL` is the address of the instance and `$TOKEN` is the token they were given. `--env dev` makes
`dev` the default environment, so that no command needs `--env`.

<!-- output: give-access#login -->
```text
Logged in as alice (USER) at http://127.0.0.1:50899; saved profile 'default'
```

Then they check who they are and run a first query:

<!-- source: docs/examples/give-access/run.sh#first -->
```bash
lakana whoami
lakana sql "SELECT count(*) AS visits FROM silver.visits"
```

<!-- output: give-access#first -->
```text
┌────────────┬───────┐
│ name       │ alice │
│ type       │ USER  │
│ task_token │       │
│ ci_token   │       │
└────────────┴───────┘
┏━━━━━━━━┓
┃ visits ┃
┡━━━━━━━━┩
│ 0      │
└────────┘
1 rows
```

The table `silver.visits` is one that this instance holds; use one of yours. If a command is
refused, the grant is missing or names another environment: look at the grants of the person, as
the next step shows.

## See who has access

List the principals, the grants of one person, who has access to the environment, and the tokens
of the person:

<!-- source: docs/examples/give-access/run.sh#who -->
```bash
lakana principal list
lakana grant list --principal alice
lakana grant list --env dev
lakana token list alice
```

The table of `grant list` names each grant's subject (`user alice`, `service ci-bot` or
`group analysts`) and shows its role. The `id` column is always shown whole, at any width: a long name
wraps onto more lines and other columns give way first. A line under the table gives the command
that revokes a grant. If a name cannot be found (reading names needs the right to list principals
and groups), the table shows the id there and a notice says so. A column that is empty for every grant
is left out, and when the terminal is too narrow a line under the table says which columns were
left out; `--output json` always has every field. `--env dev` lists who has access to one
environment, with their roles; without a filter `lakana grant list` shows every grant, newest
first, and a long list ends with the command that shows the next page. The column `last_used_at`
of `token list` shows whether a token is in use. To see what one person may do and through which
grant, read [Why may (or may not) someone do this?](why-may-someone-do-this.md). Every filter and
option is in [Grants, permissions, principals and groups](../reference/access.md).

<!-- output -->
```text
┏━━━━━━━┳━━━━━━┳━━━━━━━━━━━┓
┃ name  ┃ type ┃ is_active ┃
┡━━━━━━━╇━━━━━━╇━━━━━━━━━━━┩
│ alice │ USER │ True      │
│ admin │ USER │ True      │
└───────┴──────┴───────────┘
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ id                                   ┃ subject    ┃ role    ┃ environment ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━┩
│ c913f5e7-8bf0-4b21-9630-46de66a0082d │ user alice │ ANALYST │ dev         │
└──────────────────────────────────────┴────────────┴─────────┴─────────────┘
Not shown: kind. Use --output json to see it.
Revoke it with: lakana grant revoke c913f5e7-8bf0-4b21-9630-46de66a0082d
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ id                                   ┃ subject    ┃ role    ┃ environment ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━┩
│ c913f5e7-8bf0-4b21-9630-46de66a0082d │ user alice │ ANALYST │ dev         │
└──────────────────────────────────────┴────────────┴─────────┴─────────────┘
Not shown: kind. Use --output json to see it.
Revoke it with: lakana grant revoke c913f5e7-8bf0-4b21-9630-46de66a0082d
┏━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ key_id           ┃ name         ┃ last_used_at                     ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 634fb88f45cabc42 │ alice-laptop │ 2026-10-17T13:46:56.473099+00:00 │
└──────────────────┴──────────────┴──────────────────────────────────┘
```

## Take access away

There are three commands, from the smallest step to the largest.

**One token** (a lost laptop): revoke it. The person's other tokens and their grants stay.

<!-- source: docs/examples/give-access/run.sh#revoke-token -->
```bash
lakana token revoke alice "$KEY_ID" --yes
```

<!-- output -->
```text
Revoked token 634fb88f45cabc42
```

`$KEY_ID` is the `key_id` that `token list` showed. `--yes` skips the question that the command
asks on a terminal; without a terminal the command refuses to run without it.

**One grant** (the person stays but may do less): revoke it. Their tokens still work, but they
identify a person who may do nothing until a new grant exists.

<!-- source: docs/examples/give-access/run.sh#revoke-grant -->
```bash
lakana grant revoke "$GRANT_ID" --yes
```

<!-- output: give-access#revoke-grant -->
```text
Revoked grant c913f5e7-8bf0-4b21-9630-46de66a0082d
```

`$GRANT_ID` is the `id` that `grant list` showed.

**The whole person** (they leave): deactivate the principal. Every token of theirs stops working.
The principal and its grants stay on record, and `grant list` still shows them; revoke the grants
too if you want the list to show only people who have access.

<!-- source: docs/examples/give-access/run.sh#deactivate -->
```bash
lakana principal deactivate alice --yes
```

<!-- output: give-access#deactivate -->
```text
Deactivated principal alice
```

The saved profile stays on the person's machine, but its token no longer works; they can remove it
with `lakana logout`.

## Where next

- [Why may (or may not) someone do this?](why-may-someone-do-this.md) shows what a person may
  do on an object, and through which grant, role or group.
- [Show some people only some rows](filter-rows.md) and
  [Mask a column for some people](mask-a-column.md) narrow what a person with a grant sees.
- [Deploying with Compose](deploy-with-compose.md#revoking-the-bootstrap-credential) replaces the
  bootstrap administrator with a named one: the same commands, with the role `ADMIN` on the
  instance.
