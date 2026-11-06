# Keep access in a reviewed file

After this guide, who has access to what lives in one file in a git repository. Every change goes
through a pull request, a workflow shows what it would change before the merge and applies it after,
and the instance refuses changes made by hand in the meantime. You need the `ADMIN` role on the
instance, a git repository for the file, and (for the workflow) GitHub Actions or a similar CI.

The examples use the user `alice`, the group `finance` and the table `dev.gold.customers` with an
`email` column; replace them with yours.

Keep the file in a repository of its own, which only administrators can merge into: whoever can
merge into it decides who has access. The format and every error are in [The access
file](../reference/access-file.md).

## 1. Create the account that applies the file

An apply acts as a person or service account with the `ADMIN` role on the instance. For CI, create
a service account and give it a personal access token. Create the account first, so that the
file you export next lists it; a file that leaves it out would remove it.

<!-- source: docs/examples/access-file/run.sh#account -->
```bash
lakana principal create access-admin --type service-account
lakana grant create --principal access-admin --role ADMIN --kind INSTANCE
lakana token issue access-admin access-ci
```

Store the printed token as the repository secret `LAKANA_ACCESS_TOKEN` and the server's URL as
the variable `LAKANA_URL`. A CI token (the OIDC exchange of the deploy guides) and a task token
cannot read or change access: every `lakana access` command is refused with `403` for them.

## 2. Write the file

Export the live access once and put the file in the repository:

<!-- source: docs/examples/access-file/run.sh#export -->
```bash
lakana access export -o access.yaml
```

The file lists groups, service accounts, grants, row filters, column masks and CI trust rules.
`manages: [instance]` means the file is the whole truth: after the first apply, anything it does
not list is removed. To start smaller, export one environment (`--scope prod`) and let the file
manage only that. Edit the file: for example, add `alice` to `finance`, give `finance` the
`ANALYST` role on `dev`, and mask the `email` column for the group:

```yaml
groups:
  finance:
    members: [alice]
grants:
  - to: group:finance
    role: ANALYST
    on: dev
policies:
  - kind: column_mask
    table: dev.gold.customers
    column: email
    expression: "'***'"
    to: group:finance
```

(The block shows only the lines you add or change; the rest of the file stays as exported.)

## 3. See what it would change

<!-- source: docs/examples/access-file/run.sh#plan -->
```bash
lakana access plan access.yaml
```

The plan changes nothing. `+` is created, `-` is removed, and the fingerprint is a hash of the exact
text of the file and of the live access that `apply` checks again:

<!-- output: access-file#plan -->
```text
+ member alice of group finance  (groups.finance.members[0])
+ grant role ANALYST on environment dev to group:finance  (grants[2])
+ column mask on dev.gold.customers.email for group:finance: '***'  (policies[0])
Fingerprint: <hash>
```

Read every `-` line twice: it is access someone had and the file does not list. A problem in the
file stops the plan with exit code 1 and lists all of them.

## 4. Review in a pull request, apply after the merge

The reviewers read the diff of `access.yaml`; the workflow below shows the plan on the pull
request and applies the merged file on `main`:

```yaml
name: access
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  plan:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    env:
      LAKANA_URL: ${{ vars.LAKANA_URL }}
      LAKANA_TOKEN: ${{ secrets.LAKANA_ACCESS_TOKEN }}
    steps:
      - uses: actions/checkout@v6
      - run: pip install lakana
      - run: lakana access plan access.yaml
  apply:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    concurrency:
      group: lakana-access
      cancel-in-progress: false
    env:
      LAKANA_URL: ${{ vars.LAKANA_URL }}
      LAKANA_TOKEN: ${{ secrets.LAKANA_ACCESS_TOKEN }}
    steps:
      - uses: actions/checkout@v6
      - run: pip install lakana
      - run: lakana access apply access.yaml --yes --source-commit "$GITHUB_SHA"
```

Pin the actions to commit hashes as your organisation does. `apply --yes` plans the merged file and
applies exactly that plan in one step, in one transaction: either every change is made or none is.
The apply is recorded in the audit log with the commit. To apply a plan you reviewed earlier, pass
its fingerprint with `--plan-fingerprint`; the apply is then made without asking, since that plan
was the review. It is refused, with a message that says to plan again, if the file text or the
access changed since: what you reviewed is the file as it was and the access it was planned against.

Anyone who can merge into the repository can change access, and the secret in it is an
administrator's token. Protect the `main` branch, and restrict who can edit the workflow.

### Stricter: CI runs only the plan

If a stored administrator token in CI is more than your team wants, leave the `apply` job out. CI
runs `lakana access plan access.yaml` on pull requests, and after the merge an administrator
applies from their own profile:

<!-- source: docs/examples/access-file/run.sh#stricter -->
```bash
lakana access apply access.yaml --source-commit 0123abc
```

Without `--yes` the command shows the plan and asks before it applies. Applying with a CI token
instead of a stored personal token is a later option, tracked in
[issue 455](https://github.com/lakana-labs/lakana/issues/455).

### A project repository can also be listed

The file's `ci_trust` section creates the trust rules that let a project repository deploy
(see [Deploy from GitHub Actions](deploy-from-github-actions.md)), so they are reviewed with the
rest of the access:

```yaml
ci_trust:
  - issuer: github
    repository: analytics
    as: service:etl-bot
    on: prod
    conditions:
      repository_id: "123456789"
      ref: refs/heads/main
```

## 5. See what the file manages

<!-- source: docs/examples/access-file/run.sh#status -->
```bash
lakana access status
```

<!-- output: access-file#status -->
```text
┏━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ scope    ┃ file        ┃ commit  ┃ applied by   ┃ applied at                       ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ instance │ access.yaml │ 0123abc │ access-admin │ 2026-11-06T02:49:50.848024+00:00 │
└──────────┴─────────────┴─────────┴──────────────┴──────────────────────────────────┘
```

## 6. What a managed scope refuses

From the first apply the file is the only way to change access in its scopes. A command that
changes it by hand is refused:

<!-- source: docs/examples/access-file/run.sh#refused -->
```bash
lakana grant create --principal alice --role viewer --kind ENVIRONMENT --env dev
```

<!-- output: access-file#refused -->
```text
Error: `instance` is managed by an access file (last applied 2026-11-06T02:49Z); change the file and run `lakana access apply`, or, in an emergency, repeat with `--override-managed --reason …`
```

The way out is the file: change it, review it, apply it. In an emergency an administrator can make
the change anyway and give a reason; the change is recorded in the audit log:

<!-- source: docs/examples/access-file/run.sh#override -->
```bash
lakana grant create --principal alice --role viewer --kind ENVIRONMENT --env dev --override-managed --reason "Incident 42 hotfix"
```

The CLI warns that the next apply reverts the change unless the file lists it. The next plan shows
the change as drift:

<!-- source: docs/examples/access-file/run.sh#drift -->
```bash
lakana access plan access.yaml
```

<!-- output: access-file#drift -->
```text
- grant role VIEWER on environment dev to user:alice
! drift in instance: grant VIEWER to alice (override) — access-admin, 2026-11-06T09:45:24.453110+00:00: Incident 42 hotfix
Fingerprint: <hash>
```

The next apply removes the change again unless the file lists it, so add it to the file if it
should stay. [The override](../reference/access-file.md#the-override) lists what it needs.

## When an apply is refused

Each message names the cause and the next step; the full list is in [The access
file](../reference/access-file.md#errors). The two you will meet first: the file or the access changed
since the plan (plan again and review), and a file that removes the account that applies it (list the
account and its `ADMIN` grant, or have another administrator apply).
