# Connections

A connection is a source that the assets of an environment read from: a database, a file store,
an object store or an HTTPS API. It has a name, a kind, an address (where the source is), settings
(how to reach it) and at most one secret (the password, token or key). Each environment has its
own connections, as it has its own secrets, so `crm-db` in `dev` and `crm-db` in `prod` can point
at different servers. An asset names the connections it reads from in
`@asset(connections=[...])`; a deploy is refused while the environment lacks one of them. Inside
the asset's task, `lakana.ingest.connections.get` reads a declared connection with its secret
([How a task gets its credential](#how-a-task-gets-its-credential)).

Creating or changing a connection reaches no source: it only stores where the source is. To find
out whether the source can be reached, [test the connection](#test-a-connection). The
connection's name and kind never change; to change them, delete it and create it again. The
deleted connection's secret stays in the environment, unbound, and keeps its name, so new values
for the new connection create a secret under the next free name (`crm-db-connection-2`, below).

## Kinds

| Kind | Address | Settings | Secret keys |
|---|---|---|---|
| `postgresql` | `host`, `port` (default `5432`), `database` | `user` (required), `tls`, `ca_certificate` | `password` |
| `mariadb` | `host`, `port` (default `3306`), `database` | `user` (required), `tls`, `ca_certificate` | `password` |
| `sqlite-file` | `root`, `path` (a file) | none | none |
| `http` | `base_url` | `auth`, `username`, `api_key_name`, `api_key_in`, `client_id`, `token_url`, `scope`, `ca_certificate` | by `auth`: `token` (bearer), `password` (basic), `api_key` (api_key), `client_secret` (oauth2); none for `none` |
| `s3` | `bucket`, `region`, `endpoint` (default `https://s3.<region>.amazonaws.com`; while it is the default, a change of `region` moves it to the new region's) | `access_key_id` (required), `prefix` | `secret_access_key`, optional `session_token` |
| `gcs` | `bucket` | `prefix` | `service_account_json` |
| `azure` | `account`, `container` | `prefix` | exactly one of `account_key` and `sas_token` |
| `directory` | `root`, `path` (a directory) | none | none |
| `tcp` | `host`, `port` (both required) | none | optional, keys of your choice: 1 to 16 names of lower-case letters, digits and `_`, such as `user` and `password` |

What each setting means, and its default:

| Setting | Kinds | Meaning |
|---|---|---|
| `user` | `postgresql`, `mariadb` | The database user. Give the connection a user that may only read. |
| `tls` | `postgresql`, `mariadb` | `verify-full` (default: encrypted, and the server's certificate is checked for the host), `require` (encrypted, certificate not checked) or `disable`. |
| `ca_certificate` | `postgresql`, `mariadb`, `http` | Your CA certificates in PEM, for a server whose certificate a public CA did not sign. Every answer shows it as its SHA-256 fingerprint (`sha256:...`), never the certificate. |
| `auth` | `http` | `none` (default), `bearer`, `basic`, `api_key` or `oauth2` (client credentials). |
| `username` | `http` | The user name of `basic` (required for it). |
| `api_key_name`, `api_key_in` | `http` | The header or query parameter that carries the key of `api_key` (the name is required for it); `api_key_in` is `header` (default) or `query`. |
| `client_id`, `token_url`, `scope` | `http` | The client of `oauth2`: its id and the `https` URL of the token endpoint (both required for it), and an optional scope. |
| `access_key_id` | `s3` | The access key id; the secret holds the secret access key. |
| `prefix` | `s3`, `gcs`, `azure` | Only objects under this prefix. |

- **Names** of connections are lower-case letters, digits and `-`, start with a letter, and have
  at most 63 characters. A name is unique in its environment.
- **Hosts** are written in lower case. A port is a number from 1 to 65535.
- **URLs** (`base_url`, `endpoint`, `token_url`) start with `https://` and have no user name,
  password, `?` or `#`. An `http://` address is refused when a connection is created or changed.
- **`root`** names an ingestion root that the operator gives the worker; **`path`** is relative
  to it, with no `..` part and no leading `/`. Whether the root exists is checked when a task
  runs, not when the connection is created.
- A `tcp` connection carries whatever protocol your own driver speaks. Lakana requires no TLS
  for it: whether the traffic is encrypted is up to that driver.

## Limits

| What | Limit |
|---|---|
| A connection's name | 63 characters |
| `address` and `settings` together, as JSON | 16 KiB, not counting `ca_certificate` |
| `ca_certificate` | 64 KiB |
| One value of the secret | 64 KiB, not empty |
| `tcp` secret keys | 1 to 16 |
| URLs | `https` only |

## Who may do what

| Action | Right | Roles that have it |
|---|---|---|
| Create, change, delete and test a connection | `MANAGE_CONNECTION` on the environment | `ADMIN`, `DATA_ENGINEER` |
| List and show connections | `READ_ENVIRONMENT` on the environment | every role on it |

Testing a connection is a probe from the worker: with the local runner it dials from the worker's host
to any address the connection names and lists paths under the ingestion roots, so
`MANAGE_CONNECTION` lets a data engineer without `DEPLOY` find out which ports and paths the worker
can reach (see the [threat model](../explanation/security.md)). With the Docker runner a test runs in
a container under the network rules a task with that connection gets, so it reaches only what such a
task could.

A task cannot list or show connections, whatever its token's rights: a request with a task token
is refused (403) without naming any connection. A running task reads only the connections its
asset declares, one by one
([How a task gets its credential](#how-a-task-gets-its-credential)).

A connection's secret is given one of three ways, and who may give it depends on the way:

| How | Needs |
|---|---|
| New values (`--password-stdin` or `--values-stdin`): Lakana creates a secret `<name>-connection` that only this connection uses (or `<name>-connection-2`, `-3`, … up to `-100` when a secret, bound, unbound or revoked, holds that name, or the environment uses it for storage or a webhook; an existing secret is never overwritten), or sets the one it has again | `MANAGE_CONNECTION` |
| `--secret NAME` with the secret the connection uses already | `MANAGE_CONNECTION` |
| `--secret NAME` with any other secret, or `lakana secret bind NAME --connection CONNECTION` | also `MANAGE_SECRET` (the `ADMIN` role); the bind makes it the secret the connection uses, in one step |

A secret that another connection, a package index or a repository uses, or that the environment
uses for its storage or webhooks, is never taken by naming it: an administrator moves it on purpose
with `lakana secret bind`. `lakana secret bind` prints every `host:port` the connection sends the
secret to (an `oauth2` connection's token URL included), and `lakana secret list` shows them as
`use_target_url`; read them before you bind. A refusal sent to someone who may not read the
environment, such as a package index manager, says "a connection of environment 'prod'" without
its name.

A change that could send a connection's secret somewhere new also needs `MANAGE_SECRET`, unless
the same change gives new values: a new address (`host`, `port`, `base_url`, `endpoint`, `bucket`,
`account`), a weaker `tls`, another `ca_certificate`, another `auth` or `api_key_in`, or another
`token_url`. A data engineer who moves a database to a new host gives its password again in the
same command. A new `region` of an `s3` connection whose endpoint is the default moves the
endpoint too, so it counts as an address change.

## Test a connection

`lakana connection test crm-db --env prod` asks a worker to check that the source can be reached
with the connection's address and settings. The worker checks, in this order, the host name (DNS),
the connection (TCP), the TLS handshake with the connection's CA certificate, and, for a
`directory` or `sqlite-file` connection, that the ingestion root exists and the path is under it.
For an `http` connection with an OAuth2 `token_url` the token host is checked the same way as the
base URL, and the test is not `ok` unless both pass; the message names the host that failed. The TLS
of a `mariadb` connection is not checked yet (the check reads the server's greeting only).
Credentials are not checked yet: a connection with a secret ends its message with "credentials not
checked yet", and each kind's credential check arrives with its helper. Any worker runs the check.
With the local runner it is a subprocess of the worker's Python that needs `lakana` installed next to
the worker (without it the test fails and says so). With the Docker runner (the Compose bundle's
default) the launcher runs it in a container of the task image, from the image's own copy of the check,
with the network rules a task with that connection gets: through the egress proxy or the task's relay
to the connection's endpoints only, its ingestion root read-only, the worker's trust bundle
(`LAKANA_RUN__CA_BUNDLE`), no Lakana server on its network, 30 seconds and 256 MiB. Its token
for the egress proxy has the purpose `connection-test` and names only the connection's endpoints.
How the outcome is decided depends on the route class:

- **proxy** (`http`, `s3`, `gcs`, `azure`): the proxy answers the check only once it has reached
  the source, so its answer is the outcome;
- **relay** (`postgresql`, `mariadb`, `tcp`): the relay accepts the check's connection before it
  dials the source, so the test is `ok` only when the relay reports that it reached the source.
  The launcher waits up to 20 seconds for that report. A relay that could not reach the source
  gives `dns`, `refused`, `timeout` or `not_allowed`, with a message that starts `the relay
  refused`; a relay that reported nothing within the 20 seconds gives `timeout` ("the relay did
  not reach HOST:PORT within 20 s"). A relay refusal about the operator's side (the upstream proxy,
  the egress proxy's routing table) gives `failed`, naming `docker compose logs egress`;
- **tcp** connections take the relay route, and the check only connects, so their `ok` means the
  relay reached the source;
- **mount** (`directory`, `sqlite-file`): the root must be one the launcher mounts (else
  `root_unavailable`), and the path must exist under it (else `not_found`).

A launcher that
refuses the check or cannot be reached makes the test `failed` with the reason and the launcher's
log to read (`docker compose logs launcher`). A connection with a secret needs the worker's secrets master
key (`LAKANA_SECRETS__MASTER_KEY_FILE`, as on the server). The command waits
up to 120 seconds, prints the outcome and exits 1 unless it is `ok`. Only one test of a connection
runs at a time; asking again meanwhile is refused (409), naming the open test and the command
that shows it (`lakana connection test NAME --env ENV --id ID`). A test still queued or running
after 10 minutes is stuck: the next request finishes it as `failed` ("no worker finished this
test within 10 minutes; …", naming the worker's log to read) and queues a new one.

`--no-wait` queues the test and prints its id; `--id ID` shows that test later (or the one a
timed-out wait named). A test nobody takes within the wait ends with an error that says whether a
worker took it; a test nobody took says to check that a worker is running (`docker compose ps
worker`, or the `lakana local` terminal), and one a worker took names the worker's log (`docker compose logs worker`, or the
`lakana local` terminal). The test itself goes on.

| Outcome | What it means | What to do |
|---|---|---|
| `ok` | The source can be reached. | Nothing. |
| `dns` | The host name did not resolve. | Check the host name. |
| `refused` | The host refused the connection. | Check the port and the source's firewall. |
| `timeout` | The connection did not answer in time. | Check the host, the port and the network path between the worker and the source. |
| `tls` | The TLS handshake failed: the server's certificate is not trusted, or the server offers only TLS older than 1.2, which Lakana refuses (the message says which). | Check the CA: `lakana connection update NAME --ca-file ca.pem`. When the message names the TLS version, upgrade the server to TLS 1.2 or newer. |
| `not_allowed` | The address is one a task may not reach. | The address must be reachable from outside the Lakana stack. |
| `root_unavailable` | The worker does not have the ingestion root. | Check the root setting. |
| `not_found` | The path under the root does not exist. | Check the path under the root: `lakana connection show NAME`. |
| `credentials` | The source refused the secret. | Check the secret. |
| `failed` | The check failed in another way, or no worker finished it within 10 minutes. | See the worker's log. |

The message after the outcome is at most 1,000 characters and holds no secret value. The command
prints the next step on standard error; `--output json` prints the test as the REST answer below.

## Command line

Create a connection; `--password-stdin` reads the password from standard input, so it never
appears in the shell's history:

<!-- source: docs/examples/connections/run.sh#create -->
```bash
lakana connection create crm-db --env dev --kind postgresql \
  --host db.example.com --database crm --user reader --password-stdin < crm-password.txt
```

<!-- output: connections#create -->
```text
Created connection crm-db in dev with its secret crm-db-connection
```

A kind without a secret takes none:

<!-- source: docs/examples/connections/run.sh#create-directory -->
```bash
lakana connection create exports --env dev --kind directory --root shared --path exports
```

An option the kind does not take is refused before anything is sent, and the message names the
kind's options, for example `--host does not apply to kind s3; its options are --bucket, --region,
--endpoint, --access-key-id, --prefix, --secret, --password-stdin, --values-stdin`. The options of
each kind are those of the table above: `--host`, `--port`, `--database`, `--url` (`base_url`),
`--bucket`, `--region`, `--endpoint`, `--account`, `--container`, `--root` and `--path` for the
address; `--user`, `--tls`, `--ca-file` (a PEM file, read into `ca_certificate`), `--auth`,
`--username`, `--api-key-name`, `--api-key-in`, `--client-id`, `--token-url`, `--scope`,
`--access-key-id` and `--prefix` for the settings. `--password-stdin` stores standard input, less
one trailing line break, under the kind's single key (`password`, `token`, `api_key`,
`client_secret`, `secret_access_key`, `service_account_json` or `account_key`); `--values-stdin`
reads a JSON object of several keys, such as `{"secret_access_key": "...", "session_token":
"..."}`.

List and show:

<!-- source: docs/examples/connections/run.sh#list -->
```bash
lakana connection list --env dev
```

<!-- output: connections#list -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┓
┃ name    ┃ kind       ┃ address                                    ┃ secret            ┃ declared_by ┃
┡━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━┩
│ exports │ directory  │ root=shared path=exports                   │                   │ (none)      │
│ crm-db  │ postgresql │ host=db.example.com port=5432 database=crm │ crm-db-connection │ (none)      │
└─────────┴────────────┴────────────────────────────────────────────┴───────────────────┴─────────────┘
```

<!-- source: docs/examples/connections/run.sh#show -->
```bash
lakana connection show crm-db --env dev
```

<!-- output: connections#show -->
```text
Connection:  crm-db (postgresql)
Address:     host=db.example.com port=5432 database=crm
Settings:    user=reader tls=verify-full
Secret:      crm-db-connection
Declared by: (none)
Created:     2026-10-23T22:54:56.950729+00:00 by admin
Updated:     2026-10-23T22:54:56.954955+00:00
```

Change it; only the options given change. A new host with new values in the same command needs
only `MANAGE_CONNECTION`. `--clear secret` stops using the secret and unbinds it, and `--clear`
with a setting's name (such as `ca_certificate`) removes the setting:

<!-- source: docs/examples/connections/run.sh#update -->
```bash
lakana connection update crm-db --env dev --host db2.example.com --password-stdin < crm-password.txt
```

<!-- output: connections#update -->
```text
Updated connection crm-db in dev
```

Delete it. It asks first, unless `--yes` is given; the secret stays in the environment, unbound:

<!-- source: docs/examples/connections/run.sh#delete -->
```bash
lakana connection delete exports --env dev --yes
```

<!-- output: connections#delete -->
```text
Deleted connection exports in dev
```

The audit events of connections:

<!-- source: docs/examples/connections/run.sh#audit -->
```bash
lakana audit list --kind connection --env dev
```

`--output json` prints the record of the REST answer (below) for `show`, `create` and `update`.

## REST

| Route | Right | Answer |
|---|---|---|
| `POST /api/v1/environments/{env}/connections` | `MANAGE_CONNECTION` | 201 and the connection, whose `secret` is the name its new secret got; 409 when the name is taken; 422 naming the field that is not valid |
| `GET /api/v1/environments/{env}/connections?limit=&cursor=` | `READ_ENVIRONMENT` | `{"items": [...], "next_cursor": ...}`, newest first, `limit` from 1 to 200 (default 100) |
| `GET /api/v1/environments/{env}/connections/{name}` | `READ_ENVIRONMENT` | the connection; 404 names no other connection |
| `PATCH /api/v1/environments/{env}/connections/{name}` | `MANAGE_CONNECTION`, and `MANAGE_SECRET` as above | the connection |
| `DELETE /api/v1/environments/{env}/connections/{name}` | `MANAGE_CONNECTION` | 204; 409 while a deployed project version declares it |
| `POST /api/v1/environments/{env}/connections/{name}/tests` | `MANAGE_CONNECTION` | 202 and the test `{"id", "state", "outcome", "message", "created_at", "finished_at"}` (`state` is `queued`, `running` or `done`; `outcome` and `message` are `null` until done); 409 (naming the open test's id) while a test of the connection is queued or running and less than 10 minutes old; an older one is finished as `failed` first |
| `GET /api/v1/environments/{env}/connections/{name}/tests/{test_id}` | `MANAGE_CONNECTION` | the test; 404 for a test of another connection |
| `GET /api/v1/audit-events?kind=connection` | `READ_AUDIT_LOG` | every `CONNECTION_*` event; combines with the other filters |
| `GET /api/v1/tasks/self/connections/{name}` | the token of a running task whose asset declares `name` | `{"name", "kind", "address", "settings", "endpoints": [{"host", "port", "role"}], "secret"}`, with `Cache-Control: no-store`; 403 otherwise (below), 401 for a token whose attempt has ended or was claimed again. For the helpers of `lakana.ingest`; the SDK's `client.task_connections.get(name)` returns the body as it is |

The body of a create; `secret_values` and `secret` are both optional, and at most one is given:

```json
{
  "name": "crm-db",
  "kind": "postgresql",
  "address": {"host": "db.example.com", "database": "crm"},
  "settings": {"user": "reader"},
  "secret_values": {"password": "..."}
}
```

The body of a change holds only what changes: `address` and `settings` (a key set to `null` is
removed), `secret`, `secret_values`, and `clear`, a list of `secret` and settings names:

```json
{"address": {"host": "db2.example.com"}, "secret_values": {"password": "..."}}
```

The record, in every answer: `name`, `kind`, `address` and `settings` (with defaults filled in,
`ca_certificate` as its fingerprint), `secret` (the secret's name, or `null`), `declared_by` (a
list of `{"repository": ..., "asset": ...}`), `created_by` (a principal's name), `created_at` and
`updated_at`. No answer of these routes holds a secret's value; only the task route above does,
for the running task itself.

## SDK

```python
import lakana

client = lakana.Client()
connection = client.connections.create(
    "prod",
    "crm-db",
    kind="postgresql",
    address={"host": "db.example.com", "database": "crm"},
    settings={"user": "reader"},
    secret_values={"password": "..."},
)
print(connection.secret)  # crm-db-connection
client.connections.update("prod", "crm-db", settings={"user": "loader"})
page = client.connections.list("prod")  # one page: page.items, page.next_cursor
for each in client.connections.iter("prod"):  # every page
    print(each.name, each.declared_by)
client.connections.delete("prod", "crm-db")
client.secrets.bind("prod", "crm-reader", "connection", "crm-db")
events = client.audit_events.list(kind="connection")
```

`update` takes `address`, `settings`, `secret`, `secret_values` and `clear` (a list of names).

`client.connections.test("prod", "crm-db")` queues a test and polls it (0.5 s, growing to 2 s) until
it is done or `timeout` (120 s) passes, and returns a `ConnectionTest` with `id`, `state`,
`outcome`, `message`, `created_at` and `finished_at`. `wait=False` returns it as queued;
`test_id=` looks at an earlier test. A test nobody finished in time raises `TimeoutError`, naming
the state, the worker log to read and the command that looks again.

## How a task gets its credential

An asset reads a connection it declares with `lakana.ingest.connections.get`:

```python
import polars as pl
from lakana import asset
from lakana.ingest import connections


@asset(connections=["crm-db"])
def customers() -> pl.DataFrame:
    crm = connections.get("crm-db")
    host, user = crm.address["host"], crm.settings["user"]
    password = crm.secret["password"]  # crm.secret is None for a connection without a secret
    ...
```

- **Fetched at run time, with the task's own token.** The first `get` of an attempt asks the
  server for the connection (`GET /api/v1/tasks/self/connections/{name}`); a second `get` of the
  same name in the same attempt asks nothing. The server answers only the token of a task that is
  running now, in its own environment, for a name its deployed asset declares in
  `@asset(connections=[...])`. Any other token is refused, and so is a name the asset does not
  declare, whether or not a connection of that name exists.
- **In memory only.** The credential is kept in the task's memory for the attempt and never put
  in an environment variable, a file, the task's log, an error message, lineage or the deploy
  record. Each value of the secret is shown as `***` in what the task prints, in its failure
  message and in its trace: as it is, and as it appears in a `repr`, in JSON, URL-encoded
  (`quote`, `quote_plus`) and as the `repr` of its bytes, and a JSON form once more inside a
  `repr`, as bytes or JSON-encoded again (a JSON body printed as it is sent). `repr(crm.secret)` shows
  `{'password': '***'}`. What is not hidden: a value or form shorter than 4 characters, any
  other transformation of a value (such as base64 or a part of it), and a value your code
  derives from the secret (such as an OAuth2 access token), unless a `lakana.ingest` helper
  registers it.
- **A worker hides only what it can decrypt.** When an attempt starts, the worker reads the
  secrets of the connections the asset declares. A worker without the instance's secrets master
  key (`LAKANA_SECRETS__MASTER_KEY`, the server's value) cannot hide them in the task's log, so
  it does not start an attempt whose declared connections have a secret then: the attempt fails
  for good, naming the connection and the setting. A secret value must be text: one that
  cannot be written as UTF-8 is refused when it is created or set again (422, naming its key).
- **A rotated secret is used by the next attempt.** Nothing is kept from one attempt to the
  next, so `lakana connection update crm-db --password-stdin` takes effect when the next attempt
  starts, without a deploy. The worker reads the secrets to hide when the attempt starts. A
  secret added, bound or rotated while an attempt runs, and the secret of a declared connection
  created after the attempt started, is still hidden by the task in its failure message and
  trace, but may appear unhidden in the log the worker captures if the task prints it; on a
  worker without the master key, such a secret is not caught by the check at the start either.
  Change secrets between runs.
- **Only the secret bound to the connection.** A secret that is revoked, or bound to anything
  but this connection, counts as none: `crm.secret` is `None`.
- **Every fetch is in the audit log**, as `CONNECTION_CREDENTIAL_FETCHED`; refusals are
  `CONNECTION_CREDENTIAL_REFUSED` (`lakana audit list --kind connection`). Refusals are
  rate-limited per principal (`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`): the first of each
  reason is always recorded, the ones past the limit are counted in the next recorded event and
  named in the server's log. No event holds a value.
- **Deployed code sees its declared credentials.** Code you deploy can read the secret of every
  connection its asset declares and send it to any host the task can reach. Review an asset's
  `connections` as you review its code.

`connections.get` returns `name`, `kind`, `address`, `settings` (a `ca_certificate` in full),
`endpoints` (each `host`, `port` and `role`, `service` or `token`), `secret`, and three fields
that say how this task reaches the source: `proxy_url`, `relay_address` and `root_path`.

### What a task reaches, and how

When an attempt starts, the worker reads the connections the asset declares and gives the task
exactly their endpoints, nothing more. Under the Docker runner (`LAKANA_RUN__RUNNER=docker`) the
task has no other way out: HTTPS goes through the egress proxy with a token for those endpoints
only, databases and `tcp` through a relay of its own, and directories are read-only mounts. The
local runner connects directly.

| Kind | Docker runner | Local runner |
|---|---|---|
| `http`, `s3`, `gcs`, `azure` | `proxy_url` is the egress proxy; give it to your HTTPS client. It holds the task's proxy token, so it is left out of the connection's `repr` and shown as `***` in the task's log. `relay_address` and `root_path` are `None`. | Every field is `None`: connect to `address` directly. |
| `postgresql`, `mariadb`, `tcp` | `relay_address` is `lakana-relay:<port>` (ports from 15001, in the order the asset declares its connections); connect there instead of `address["host"]`. TLS still checks the real host name. | `relay_address` is `None`: connect to `address` directly. |
| `directory`, `sqlite-file` | `root_path` is `/ingest/<root>/<path>`, read-only. | `root_path` is `<directory>/<path>`, where `<directory>` is the root's entry in `LAKANA_RUN__INGESTION_ROOTS`. |

An asset declares at most 16 connections (the deploy refuses more), and a connection has at most
two endpoints (an `http` connection's base URL and token URL), so a task reaches at most 32 hosts
through the egress proxy and 16 database endpoints through its relay, and mounts at most 16
ingestion roots. One more limit: the hosts and ports of a task's HTTPS connections travel in one
egress token of at most 8192 characters, which only very long host names exceed. An asset over a
limit fails without retry, naming it: split its connections across several assets.

**A connection changed during an attempt is retried.** The task gets the endpoints the worker
read when the attempt started. If `lakana connection update` changes where the connection points
(a host or port, the root or path of a `directory` or `sqlite-file` connection, or its kind, when
it was deleted and created again) while the
attempt runs, its first `get` of that connection fails with ``connection `crm-db`
changed after this attempt started; the next attempt uses the new address``, keeps nothing, and
the attempt is retried while the asset has retries left (`retries:`, as for any transient
failure); the next attempt is given the new address.

## Lineage

A table or artifact written by an asset that declares connections shows each connection as a
source in its lineage. `lakana table lineage` prints it as `connection crm-db (postgresql)`, and
as `connection crm-db (deleted)` once the connection is gone. In the REST answer and the SDK the
node has `kind` `CONNECTION`, the connection's name and id, and its kind in `connection_kind`.

Lakana records these sources itself, from the asset the deploy recorded, when the asset's task
writes; nothing a task or a client sends can add one. A connection that was deleted after the
deploy is left out of the write's lineage. A write with your own token records none.

A connection appears in lineage only to someone who may list the environment's connections
(`READ_ENVIRONMENT`, see [Who may do what](#who-may-do-what)), never to a task. Everyone else sees
it counted among the hidden nodes, without its name.

## Audit events

| Event | When | Detail |
|---|---|---|
| `CONNECTION_CREATED` | A connection was created. | `name`, `kind`, `address`, `secret` (its name) |
| `CONNECTION_UPDATED` | A connection changed. | `name`, `changed` (such as `address.host`, `settings.tls`, `secret`), `address`, `secret` |
| `CONNECTION_DELETED` | A connection was deleted. | `name`, `kind` |
| `CONNECTION_TESTED` | A test was requested (`state` is `queued`) and when it finished (`state` is `done`, with `outcome`). | `connection`, `environment`, `test_id`, `state`; `outcome` once done |
| `CONNECTION_CREDENTIAL_FETCHED` | A running task fetched a connection it declares. | `connection`, `environment`, `repository`, `asset`, `run_id`, `task_run_id`, `attempt`; `reason` (`secret_not_bound` or `secret_unreadable`) when it got no secret |
| `CONNECTION_CREDENTIAL_REFUSED` | A fetch was refused. Recorded at most `LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE` times a minute per principal, the first of each reason always; the others are counted in `suppressed` of the next one recorded. | the same fields as far as they are known, and `reason`: `not_bound`, `not_declared` or `not_in_environment` |
| `SECRET_CREATED`, `SECRET_BOUND` | New values created the connection's secret, or a secret was bound to a connection or let go of it. | `use` is `connection`; `url` of `SECRET_BOUND` is every `host:port` the connection sends the secret to, such as `api.example.com:443, login.example.org:443` for an `oauth2` connection whose token URL is on another host |

No event holds a secret's value or a CA certificate. `lakana audit list --kind connection` lists
every `CONNECTION_*` event.

## Errors

| Message | What to do |
|---|---|
| ``connection names are lower-case letters, digits and '-', starting with a letter, at most 63`` | Choose a name that follows the rule. |
| `` environment `prod` already has a connection `crm-db`; change it with `lakana connection update crm-db --env prod` `` | Change the one that exists, or choose another name. |
| ``secrets `crm-db-connection` to `crm-db-connection-100` all exist in `prod`; give the connection another name, or create it without new values and ask an administrator to bind a secret to it: `lakana secret bind NAME --env prod --connection crm-db` `` | A hundred secrets hold the names a new secret could take. Choose another connection name, or create the connection without new values and ask an administrator to bind a secret to it. |
| ``give either --secret or --password-stdin/--values-stdin, not both`` | Give the secret one way. |
| ``environment `prod` has no connection `crm-db`; `lakana connection list --env prod` lists them`` | Check the name and the environment. |
| ``a task token cannot list or show connections; read them with your own token`` | A task token was used. Read connections with your own token. |
| ``connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version without it first.`` | Remove the connection from the asset's `@asset(connections=[...])`, deploy, and delete again. |
| ``Secret 'crm-pw' of environment 'prod' is not bound to connection 'crm-db'. Ask someone who may manage secrets in 'prod' to bind it to this connection: lakana secret bind crm-pw --env prod --connection crm-db (the bind also makes it the secret the connection uses)`` | An administrator runs the command; nothing else is needed. Or give the connection new values with `--password-stdin`. |
| ``This change of connection 'crm-db' (its address, TLS, CA certificate or how it sends its secret) could send secret 'crm-db-connection' of environment 'prod' somewhere new. ...`` | Give new values in the same command (`--password-stdin` or `--values-stdin`), or ask an administrator to make the change. |
| ``address.base_url ...`` (or another field) | The message names the field and its rule; fix that value. |
| ``the value of `password` is not valid text (it cannot be written as UTF-8); give it again as plain text`` | The value holds a character that is not text (a lone surrogate, such as `\ud800` in JSON). Give the value again, as plain text. |
| ``connection `crm-db` is not declared by asset `bronze.customers` `` (403) | The task asked for a connection its deployed asset does not declare. Add it to `@asset(connections=[...])` and deploy. |
| ``connection `crm-db` is not in environment `dev` `` (403) | The asset declares it, but the task's environment has no connection of that name (it was deleted since the deploy). Create it: `lakana connection create crm-db --env dev ...`. |
| ``only a running task can fetch a connection`` (403) | Only a task's own token, while the task runs, may fetch a credential. Read connections with `lakana connection show` instead; it never shows the secret. |
| ``connection `crm-db` changed after this attempt started; the next attempt uses the new address`` | The connection's address (or a directory's root or path, or its kind) changed while the attempt ran. The attempt is retried with the new address while the asset has retries left. To avoid it, change connections between runs. |
| ``` `exports` names ingestion root `shared`, which this worker does not have. Set it in `LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` (local runner). ``` | The connection's `root` is not set on the worker that took the task. An operator adds it to the setting the message names for that worker's runner and restarts it ([Ingestion roots](../how-to/run-python-tasks-in-containers.md#ingestion-roots)). Not retried. |
| ``an asset declares at most 16 connections`` (the deploy is refused) | One asset declares more connections than a task can be given. Split it into several assets. The limits of the hosts, database endpoints and ingestion roots one task gets follow from it and cannot be reached through a deploy (at most 32 hosts, 16 endpoints, 16 roots), so the worker's own refusals (``asset `x` reaches 65 hosts through the egress proxy; a task may reach at most 64 — split the asset``, ``… 17 database endpoints; a task's relay forwards at most 16``, ``… 17 ingestion roots; a task mounts at most 16``) are a safeguard you should not meet. They are not retried. |
| ``asset `x` declares more or longer host names than one task's egress token can carry (… characters of payload, at most 8192); split its connections across several assets`` | The host names of the asset's HTTPS connections are too long, together, for one task's egress token. Split the connections across several assets. Not retried. |
| ``` `crm-db` is not declared by this asset: add it to @asset(connections=[...]) and deploy. ``` | `connections.get` was asked for a name the asset does not declare; nothing was sent. Add the name and deploy. This error is not retried. |
