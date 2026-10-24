# Manage secrets

A secret holds credentials, such as the keys of a storage backend or a repository's token. Lakana
stores it encrypted in its database, and no route, command or page returns its value to a person
again; only a running task gets the secret of a connection its asset declares
([How a task gets its credential](../reference/connections.md#how-a-task-gets-its-credential)). This
page is for the person who runs the instance. It shows which secrets are stored in an older format
and how to bring them up to date.

## Webhook secrets

A job's `notify:` names a secret that holds its webhook. The secret is a JSON object with the key
`url`, the address of the receiver, and, for the `json` format, an optional `signing_key` that
signs each message. The command that creates one is in
[Get told when a job fails](get-told-when-a-job-fails.md#create-the-secret).

The worker decrypts the secret only while it sends an attempt. Nothing shows the URL or the key
afterwards: not a log, not the audit log, not the state of a delivery. To change either, set the
secret again with `lakana secret rotate NAME --env ENV --values-stdin`. Anyone who may deploy to an
environment can name a secret of it as a webhook if the secret is unbound or bound to the environment, but that reveals no value: the worker only
posts the message to it. A secret that is bound to an index or a repository is refused as a webhook. What a message holds, how to test the webhook and how a receiver checks
the signature are in the same guide.

## Bind a secret to its use

A secret has one use: the credential of one package index, of one repository, of one
connection of its environment, or its environment's own work (storage and webhooks). Lakana refuses it for anything else, so
someone who may register package indexes cannot send another secret to a host of their choice.
Under the current runner, someone who also holds `DEPLOY` and `RUN` reads what the worker reads
anyway, so binding protects against an index manager redirecting a secret, not against code that
runs in the worker. Only someone who may manage the environment's secrets (the `ADMIN` role)
binds a secret. `lakana secret bind` and `lakana secret create --index` print the name and the
URL of the index or repository: that URL is where the secret goes, so read it before you bind.

A secret serves one entry. Where several indexes or repositories shared one credential, create
one secret per entry (the same value is fine) and point each entry at its own:
`lakana secret create NEW --env ENV --repository REPO --values-stdin`, then
`lakana repo update REPO --credential-secret NEW` (`--index INDEX` and `lakana index update
INDEX` for an index). The refusal for a secret bound to another entry leads with these two
commands; moving the secret instead breaks the entry it served.

The one-step way is to create the secret with its use. Register the index first, then create the
secret bound to it (`--repository NAME` and `--own-env` work the same way):

<!-- source: docs/examples/bind-a-secret/run.sh#create -->
```bash
lakana secret create pypi-corp --env dev --index corp \
  --value username=ci --value password=not-a-real-one
```

The other way is to create the secret first and name it where it is used. An administrator who
registers or updates the index with `--credential-secret` binds each unbound secret of that name
in the same step, as long as they may manage secrets in every environment that has one. The
how-to [Use a private package index](private-package-index.md) shows that order.

`lakana secret list` shows each secret's use:

<!-- source: docs/examples/bind-a-secret/run.sh#list -->
```bash
lakana secret list --env dev
```

To bind a secret on its own, or to move it, an administrator runs `lakana secret bind` with
exactly one of `--index NAME`, `--repository NAME`, `--connection NAME` or `--own-env`:

<!-- source: docs/examples/bind-a-secret/run.sh#bind -->
```bash
lakana secret bind hook --env dev --own-env
```

A connection's own secret is usually created with the connection (`lakana connection create …
--password-stdin`); binding an existing secret to a connection needs the right to manage secrets.
The bind also makes it the secret the connection uses, in one step, and the connection lets go of
the secret it used before:

<!-- source: docs/examples/connections/run.sh#bind -->
```bash
lakana secret bind crm-reader --env dev --connection crm-db
```

<!-- output: connections#bind -->
```text
Bound secret crm-reader in dev to connection crm-db (db2.example.com:5432); it is now the secret the connection uses
```

The reference page [Connections](../reference/connections.md) lists who may give a connection
which secret.

Someone who may only register indexes or repositories (`MANAGE_PACKAGE_INDEX`,
`MANAGE_REPOSITORY`) can name a secret only if it is already bound to that same index or
repository. Any other name is refused, and the refusal is recorded in the audit log as
`AUTHORIZATION_DENIED`. The message gives the command and says to ask someone who manages
secrets:

<!-- output -->
```text
Error: Secret 'pypi-corp' of environment 'prod' is not bound to package index 'corp'. Ask someone who may manage secrets in 'prod' to bind it to this index: lakana secret bind pypi-corp --env prod --index corp
```

Creating a secret with `--index` or `--repository` is refused when its environment already uses
that name for storage or webhooks; create it with `--own-env`, or give the index a secret under
another name. `lakana secret bind` moves such a secret on purpose.

The same check guards the other places a secret is read: a deploy of a repository refuses a
secret that is not bound to that repository (`lakana secret bind NAME --env ENV --repository
REPO`), and the environment's storage refuses one bound to an index or a repository
(`--own-env` moves it on purpose, or give the storage its own secret). The message names the
secret, what it is bound to now and the command.

An unbound secret works for its environment's storage and webhooks. A secret that its
environment uses for them is refused when anyone names it from an index or a repository, with
the command that moves it on purpose. The URL of an index or repository with a bound secret
changes only with the right to manage secrets in that secret's environment. The same holds for
the storage options of an environment that has a storage secret: `lakana env set-storage ENV
--option KEY=VALUE` needs `MANAGE_SECRET` in `ENV` (the `ADMIN` role) as well as
`MANAGE_ENVIRONMENT`, because an option such as `s3.endpoint` decides where the secret is sent.
`--option` replaces all the options, so pass every option the environment should have (for
example `--option s3.endpoint=URL --option s3.region=REGION`). Sending the options the
environment already has changes nothing and needs no extra right.

Attaching a secret to an environment (`--secret-ref`) approves the options it has then: check
`s3.endpoint` first. The audit event records that endpoint.

## See how each secret is stored

List the secrets of an environment, and set one again to store it in the current format (it needs no read of the old value):

<!-- not tested: needs a running instance with a secret, and NAME and ENV are placeholders -->
```bash
lakana secret list --env ENV
lakana secret rotate NAME --env ENV --values-stdin < values.json
```

The listing shows, for each secret:

| Column | What it says |
|---|---|
| `name`, `environment` | Which secret it is. |
| `format_version` | `2` is the current format. `1` is the original one, from before the format was recorded; Lakana refuses it unless `LAKANA_SECRETS__ACCEPT_FORMAT_1` is on (see below). |
| `key_id` | Which key provider and which key wrapped the secret's data key: `env:` and 16 characters for a master key from `LAKANA_SECRETS__MASTER_KEY`, or just `env` for a secret stored in the original format. It does not reveal the key. |
| `use`, `use_target` | What the secret is bound to: `package_index`, `repository`, `connection` or `environment`, and the name of the index, repository or connection. Both are empty when it is unbound. `use_target` is also empty for `environment`, and for an index or repository that was removed; `use` then still names the kind. A connection's secret lives in the connection's environment; deleting the connection, or clearing its secret, leaves the secret unbound. |
| `created_by`, `created_at` | Who created it and when. |

## Secrets stored in the old format

A secret at format 2 is tied to its own row: its encrypted value and its encrypted data key cannot
be copied onto another secret and still decrypt, even by someone who can write to the database.
A secret at format 1 is not tied to anything. The format is a column of the row, so someone who can
write to the database could copy an old-format secret onto any row and mark it as format 1. Lakana
therefore **does not read a secret at format 1 by default**. After an upgrade from a release that
stored secrets in the old format, the first deploy or query that needs such a secret fails with:

<!-- output -->
```text
Secret 'git-token' in environment 'dev' is stored in the old format (1), which Lakana does not read by default. Set it again with `lakana secret rotate git-token --env dev --values-stdin`; that needs no read of the old value. To keep old secrets working until you have, start the server and the workers with LAKANA_SECRETS__ACCEPT_FORMAT_1=true, and remove the setting afterwards.
```

You have two ways forward, in this order:

1. **Set each secret again** (`lakana secret rotate`, as in the first section). It takes the values
   you give it, stores them at format 2, and needs no read of the old value, so it works with the
   setting off. Do this for every secret whose `format_version` is `1`. If you can do this before
   anything needs the secrets, you never need the setting.
2. **Or keep the old secrets working while you do it.** Start the server and every worker with
   `LAKANA_SECRETS__ACCEPT_FORMAT_1=true` (in the bundle, see
   [Secrets settings](deploy-with-compose.md#secrets-settings)), set the secrets again, then
   remove the setting and restart. While it is on, the server and each worker log a `WARNING` at
   start-up, because the binding is weaker: any old-format ciphertext written under your master key
   is accepted on any row, even for a secret you have already set again. The setting only
   goes off when you remove it.

A Compose bundle sets its own storage secret (`seaweedfs-s3` in `dev`) again at its next start,
because it knows the values; every secret you created yourself is yours to set again. Lakana does
not do that by itself: it could not tell an old secret from an
old-format ciphertext that someone pasted onto a row, and the migration has no master key to read
the secrets with. If a credential mattered, also rotate it at its provider (a new access key, a new
token): that is the one step that makes a copy of the old ciphertext worthless.

Lakana does not count how often a secret was set. Someone who can write to the database can still
put back an older row of the same secret from a backup, and it decrypts. Treat database write access
as access to the secrets that were ever stored there under the same master key.

## When a secret cannot be decrypted

A secret that no longer decrypts is refused with a message that names it:

<!-- output -->
```text
Secret 'git-token' in environment 'dev' could not be decrypted. If LAKANA_SECRETS__MASTER_KEY was changed, put back the key the secret was stored under. Otherwise restore it from a backup, or set it again with `lakana secret rotate git-token --env dev --values-stdin`.
```

Check the master key first: it is the usual cause, and putting back the key the secret was stored
under is the fix. Otherwise restore the database from a backup, or set the secret again with its
values. Other causes are a row that was altered or restored from another database, and a row copied
from another secret.

The log of the process that failed has one `WARNING` line with the secret's id, `format_version`,
`key_id` and `configured_key_id`. For a deploy that process is the worker (`docker compose logs
worker`), for a query or a storage request it is the server (`docker compose logs server`). For a
secret at format 2, a `key_id` that differs from `configured_key_id` means the master key is not the
one the secret was stored under; equal ids mean the row was altered or moved. The message you see
never says which.

A secret written by a newer Lakana than the one that reads it is refused with a message that says
so; upgrade Lakana, and do not set the secret again.

## See also

- [Upgrade to the release with artifacts](upgrade-to-artifacts.md): the migration that records the
  format and the key, and what going back does to secrets.
- [Deploying with Compose](deploy-with-compose.md): the master key and backups.
