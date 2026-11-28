# Organise tables in namespaces

After this guide you can create a namespace, see the namespaces of an environment, and delete one
that is empty, on the command line or in the web interface, also in a protected environment. You
need an environment (see [Getting started](../tutorials/getting-started.md)).

## Why namespaces

A namespace groups tables inside an environment, and its name is part of every table's name:
`gold.sales.orders` is the table `orders` in the namespace `gold.sales`. A namespace can sit below
another one, with a dot between them.

- **Layers.** A new environment has `bronze`, `silver` and `gold`, one for each layer of the
  medallion pattern: raw data, cleaned data, and the tables people read (see
  [Model a star schema](model-a-star-schema.md)). A table's namespace starts with one of them.
- **Teams and subjects.** Below a layer, a namespace per team or subject keeps their tables
  together, such as `gold.sales` and `gold.finance`. Access can be granted on a whole namespace,
  so a team can be given everything in its own.

A project's runs create the namespaces its assets write to. Create one by hand when you load,
register or write a table there yourself.

## Create a namespace

Creating a namespace needs `CREATE_NAMESPACE` on the environment or on the namespace above it:

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#create -->
```bash
lakana ns create gold.sales --env dev
```

The path is dotted, such as `gold.sales`. A namespace that exists already is refused with
`Namespace gold.sales already exists in 'dev'.` A new part of the path may not start or end with
a space: `" gold"` would look like `gold` in every list, so it is refused with
`Namespace component ' gold' starts with a space, so it would look like 'gold' in every list but
be a different name. Use 'gold' instead, or another name without a space at either end.` A new part
may hold only letters, decimal digits, `_`, `-` and the plain space, in Unicode NFC (see
[the upgrade note](../explanation/upgrades.md#notes-for-0230)). Properties, free-form `key=value` pairs, are set with `--property` on the
command line.

**In the web interface,** open **Catalog** and choose the environment, or the namespace to create
one beneath, in the tree. **Actions** above the tree shows what you may do there; choose **New
namespace…**. The **Path** field starts with the chosen namespace and a dot (`gold.`), or empty
under an environment: complete it and press **Create**. The Catalog opens the new namespace and
says "Created namespace gold.sales." once: a reload or Back does not show it again. A refusal
stays in the dialog, word for word, with the path as you typed it. The web interface creates a
namespace without properties.

Every dialog that changes something first asks the server whether the environment is protected.
**Create**, or any other confirm button, pressed before that answer is in shows "Checking whether
the environment is protected…" and acts once it knows; in a protected environment it asks for the
reason first instead. If the check fails or is refused, nothing is sent: press the button again.

If you may create namespaces but not read them, the tree shows a namespace only once it holds a
table you may read. The Catalog then stays where it was and says so: "Created namespace
gold.sales. You may not read the namespaces of dev, so the tree does not show it while it holds no
table you may read."

## See the namespaces

**In the web interface,** open **Catalog**. Its tree shows the environments and namespaces you may
read, with the number of tables you may read in each.

On the command line, list the namespaces of an environment:

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#list -->
```bash
lakana ns list --env dev
```

<!-- output: organise-tables-in-namespaces#list -->
```text
┏━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ path       ┃ properties ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ bronze     │ {}         │
│ gold       │ {}         │
│ gold.sales │ {}         │
│ silver     │ {}         │
└────────────┴────────────┘
```

Only the namespaces directly below one:

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#list-parent -->
```bash
lakana ns list --env dev --parent gold --depth 1
```

## Delete an empty namespace

Only an empty namespace can be deleted: drop its tables, views and artifacts, and delete the
namespaces below it, first. Deleting it needs `DROP_NAMESPACE` on the namespace, and the grants
given on it are revoked. The command asks before it deletes; `--yes` answers for you:

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#delete -->
```bash
lakana ns delete gold.sales --env dev --yes
```

A namespace that is not empty is refused, and the sentence says what it holds:

- `gold.sales still holds 2 tables.` Drop them first (see
  [Drop and restore a table](drop-and-restore-a-table.md)).
- ``gold.sales still holds 1 artifact; drop each first (`lakana artifact drop`).``
- `gold.sales still holds 1 namespace.` Delete the namespaces below it first.
- `gold.sales is not empty; drop what it holds first.` It holds something you may not read; ask
  someone who may read it what it is.

**In the web interface,** choose the namespace in the Catalog's tree and **Delete namespace…**
under **Actions**. While the tree shows tables or namespaces below it, the item cannot be chosen
and the menu says "Only an empty namespace can be deleted."; a screen reader hears it when the menu
opens. The tree counts only the tables you may read, so the server may still refuse; the dialog
then shows its sentence, as above, and stays open. An artifact is dropped from its own page, with
**Drop…** under **Actions** (see
[Drop the whole artifact](delete-an-artifact-version.md#drop-the-whole-artifact)). The dialog asks "Delete namespace gold.sales?";
press **Delete the namespace**. From a browser the server asks for a passkey touch within the last
15 minutes: touch your passkey when the browser asks, and the delete is sent again.
Someone who signs in through the organisation's identity provider confirms there instead, then
presses the action again
([single sign-on](bring-your-team-onto-single-sign-on.md#what-linking-changes-for-a-person)). If the
touch fails, the dialog stays open with the reason and nothing is deleted. The Catalog then opens
the namespace above, or the environment, with fresh counts, and says "Deleted namespace gold.sales."

## In a protected environment

A protected environment accepts writes only from runs of the deployed code (see
[Projects](projects.md#protected-environments-lockdown-and-break-glass)). To create or delete a
namespace there you need `MANAGE_ENVIRONMENT` as well, and a reason of 10 to 500 characters on one
line, which the audit log records:

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#protected-create -->
```bash
lakana ns create gold.finance --env prod --break-glass --reason "Quarter close needs a finance namespace"
```

<!-- source: docs/examples/organise-tables-in-namespaces/run.sh#protected-delete -->
```bash
lakana ns delete gold.finance --env prod --yes --break-glass --reason "Created in the wrong environment"
```

Each command warns that the write is audited. Without the two options the server refuses:
``Environment 'prod' is protected: only runs of the deployed project version may write. See `lakana run`.``

**In the web interface,** the **New namespace** and **Delete namespace** dialogs of a protected
environment ask "Why you break the protection of prod", the same reason `--reason` takes, and the
button can be pressed once it is given. A pasted reason stays on one line.

## Through the APIs

- **REST.** `POST /api/v1/environments/{env}/namespaces` with `{"path": "gold.sales",
  "properties": {}}` creates one; `GET /api/v1/environments/{env}/namespaces` lists them;
  `DELETE /api/v1/environments/{env}/namespaces/{path}` deletes one, and from the web interface asks
  for a passkey touch within the last 15 minutes (403 `StepUpRequired`), a token never does. In a
  protected environment the create takes `break_glass_reason` in its body, the delete as
  `?break_glass_reason=`. `GET /api/v1/tables/tree` says for each place what you may do there
  (see [Table list](../reference/table-list.md)).
- **SDK.** `client.namespaces.create(env, "gold.sales")`, `client.namespaces.list(env)` and
  `client.namespaces.delete(env, "gold.sales")`, each with `break_glass_reason=` for a protected
  environment.

## What the audit log records

`NAMESPACE_CREATED` for a namespace created, and `NAMESPACE_DROPPED` for one deleted, with the
number of grants it revoked, by the person who did it, from the command line, the SDK or the web
interface alike. A write with a break-glass reason also records `BREAK_GLASS_WRITE` (see
[Export and keep the audit log](export-and-keep-the-audit-log.md)).
