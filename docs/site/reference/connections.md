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
| See a connection's last test: its outcome and when, for `READ_ENVIRONMENT`; its message and who ran it, for `MANAGE_CONNECTION` | `READ_ENVIRONMENT`; `MANAGE_CONNECTION` for the message and who ran it | every role on it; `ADMIN` and `DATA_ENGINEER` for the message and who ran it |

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
base URL, and the test is not `ok` unless both pass; the message names the host that failed.
Then, for a connection with a secret, the credentials:

- `postgresql`: a login, with `SELECT 1` run in a read-only transaction. Known limit: a refused
  login is recognised by PostgreSQL's English words only (libpq does not keep its error code), so
  a server whose messages are in another language makes a wrong password `failed`, not
  `credentials`; the message then ends with "if this is a refused login on a server whose
  messages are not in English, check the user and password".
- `mariadb`: a login over TLS as the connection's `tls` says (`verify-full` checks the certificate,
  `require` uses TLS without checking it, `disable` uses none; a server without TLS is `tls`, never
  a quiet fallback to plain text), with `SELECT 1` in a read-only transaction. A `mariadb`
  connection without a secret is checked by the server's greeting only: its TLS and credentials
  are not checked, and the message says so.
- `s3`, `azure` and `gcs`: a listing of at most one object under the connection's prefix (an `s3`
  request is signed with SigV4, an `azure` one with the account key or the SAS token). A `gcs` key
  signs a token request that goes to Google's token endpoint (`oauth2.googleapis.com`) only: a key
  whose `token_uri` names another host is `credentials`, and nothing is sent.
- `http`: one `GET` of the base URL with the connection's auth (bearer, basic or the API key, in
  its header or query); `oauth2` first asks the connection's `token_url` for a token with the
  client credentials (the client secret goes in the form body, to that URL only) and then sends
  the token. A 2xx answer is "credentials accepted"; 401 or 403, or a token endpoint that refuses
  the client (400 or 401), is `credentials`; a redirect, 429 or 5xx is `failed`. Any other answer
  (a 404 from a base URL that has no page of its own, say) is `ok` with "credentials not refused":
  the server did not refuse them, but did not prove them either. A secret value with a line
  break, another control character or a character outside ASCII is `credentials`, and nothing is
  sent.

A `sqlite-file` connection has no secret: its file is opened read-only as a SQLite database, and a
file that is none fails naming the path. A refused login or key is `credentials`, with the server's
or the store's own words; the password, the key and any token never appear in the message. A `tcp`
connection is checked for reachability only. Any worker runs the check.

With the local runner it is a subprocess of the worker's Python, which needs `lakana` installed next
to the worker (without it the test fails and says so), and `lakana[assets]` for `postgresql`,
`mariadb` and `sqlite-file` connections (a `gcs` key needs nothing more: the worker brings
`cryptography`). Without the drivers the test is `failed`, and its message names the module the worker's Python cannot import and what to install
(``... cannot import `adbc_driver_postgresql`; install lakana[assets] next to the worker ...``);
it never passes without checking.

With the Docker runner (the Compose bundle's
default) the launcher runs it in a container of the task image, from the image's own copy of the check
and on the image's check environment (`/opt/lakana-check-env`, which holds exactly the drivers the
check needs), with the network rules a task with that connection gets: through the egress proxy or the
task's relay to the connection's endpoints only, its ingestion root read-only, the worker's trust bundle
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
that shows it (`lakana connection test NAME --env ENV --id ID`). Test all (below) never answers
409 for an open test. A test still queued or running after 10 minutes is stuck: the next request finishes it as
`failed` ("no worker finished this test within 10 minutes; …", naming the worker's log to read)
and queues a new one.

`lakana connection test --all --env prod` tests every connection of the environment at once
(`MANAGE_CONNECTION`, as for one). It queues a test of each, waits up to 120 seconds for all of
them, prints one line per connection, sorted by name (`crm-db: refused: <message>`), with each
outcome's next step on standard error, and exits 1 unless every outcome is `ok`. A connection
whose test is already queued or running, and under 10 minutes old, is not queued again: its open
test is the one waited for. A wait that runs out names every connection still open and the
`lakana connection test NAME --env ENV --id ID` that looks at its test. With `--no-wait` it
prints `<name>: queued <id>` or `<name>: already open <id>` and exits 0. It asks the server 200
connections at a time; NAME, and `--id`, are refused with `--all`.

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
| `not_found` | The path under the root does not exist, or the bucket or container of an `s3` or `azure` connection does not. | Check the path under the root, or the bucket or container: `lakana connection show NAME`. |
| `credentials` | The source refused the secret. | Check the secret; give a new one with `lakana connection update NAME --values-stdin` (`--password-stdin` for a one-key secret). |
| `failed` | The check failed in another way, or no worker finished it within 10 minutes. | See the worker's log. |

The message after the outcome is at most 1,000 characters and holds no secret value. The command
prints the next step on standard error; `--output json` prints the test as the REST answer below.

## In the web interface

**Connections** in the menu does what the `lakana connection` commands do, through the same REST
routes ([REST](#rest)). The screen asks the server what you may do and shows only that: a button
you may not use is not there. The address holds the place, so a link to a connection's page, to
Add or to Edit works for anyone who may open it: `/connections`, `/connections/new`,
`/connections/ENV/NAME` and `/connections/ENV/NAME/edit`.

| You hold | The screen shows you |
|---|---|
| `READ_ENVIRONMENT` on an environment | Its connections in the list, a connection's page, and each last test's outcome and time. No Add, Test, Edit or Delete, and no one's name or message from a test. |
| `MANAGE_CONNECTION` on it | **Add connection**, **Test**, **Test all**, **Edit** and **Delete…**; on a connection's page the test's message and who ran it. |
| `MANAGE_SECRET` on it too (the `ADMIN` role) | In Add and Edit, **An existing secret of ENV** and **Another secret of ENV**; and **Keep** of a secret stays open when a change moves it. |

Someone who may read no environment sees "You may not read the connections of any environment
yet. Ask an administrator for READ_ENVIRONMENT on one." and the command that lists them.

### The list

**Environment** filters the list: **All environments**, or one of those you may read. The screen
remembers the choice for you in this browser, and falls back to all environments when the one it
remembers is gone. A protected environment says so wherever the screen names it, in the filter,
the rows, Add and a connection's page: "● prod · protected", as in Queries and Jobs. When the
server cannot answer which environments are protected (it is unavailable, or does not answer),
the screen names every environment plainly, without the mark, and shows no error; it marks them
again the next time it reads. A refusal is not that: a 401 sends you to sign in, and a 403 is
shown as the refusal of the connections themselves. A connection's page names its environment
plainly whenever that read fails: a refusal there would already have refused the page's own read
of the connection. **Add connection**, the one primary action, is at the top right when you may
add a connection in any environment. Each row shows:

- **Name**, a link that opens the connection's page, and **Kind** by its name (PostgreSQL, MariaDB,
  SQLite file, HTTP API, S3, Google Cloud Storage, Azure Blob Storage, Directory, TCP);
- **Where** it points, such as `db.example.com:5432/crm` or `s3://exports (eu-central-1)`, and
  the **Environment**;
- **Status**, the last test (below);
- **Used by**, how many assets declare it ("2 assets", "1 asset", "No assets"), and, where you
  may test it, **Test**.

On a narrow window **Where** and **Used by** move under the name. The list holds 100 connections
at a time; **Show more** reads the next ones.

Every status says when the test finished, and a glyph goes with each word, so the colour never
stands alone:

| Status | Means |
|---|---|
| never tested | No test was ever asked for. |
| queued | A test waits for a worker. After 2 minutes it reads "queued · no worker yet". |
| testing… | A worker is running the test. |
| OK · 2 hours ago | The source could be reached at that time. |
| a word · 1 day ago | The test found something. The word is the outcome: host name, refused, timeout, TLS, not allowed, root missing, not found, credentials or failed ([the outcomes](#test-a-connection)). An outcome a newer server adds reads "failed". |
| tested before the last change · 3 days ago | The connection was changed after its last test, so the result says nothing about it now. It has no glyph and is shown in a muted colour. |

Under the status, a connection that a run failed on says "✗ last run failed on it · 2 hours ago";
the words open that task in its run, whose page says why. See [A run that failed on a
connection](#a-run-that-failed-on-a-connection) for when it shows.

With no connection yet, the list says "No connections yet." (or "No connections in ENV yet.") and
shows the command that creates one, and **Add connection** where you may.

### Test and Test all

**Test** on a row asks a worker for the check [described above](#test-a-connection), as
`lakana connection test` does. While a test of the connection is queued or running, the button is
disabled and reads "Testing…"; a reload finds the open test and keeps it that way, so a second
test cannot be asked for by accident.

The screen follows every open test it shows, whoever started it: it reads the connection every
second for the first 10 seconds and every 3 seconds after, and the row changes to its outcome.
When three reads in a row fail it stops with "Could not read the test's progress." and **Try
again**. A test no worker took in 2 minutes reads "queued · no worker yet", and the connection's
page adds "No worker has taken this test yet. Check that a worker is running: docker compose ps
worker, or the lakana local terminal." A test open for 10 minutes stops being followed and the
screen says "No worker finished this test within 10 minutes; Test again closes it as failed and
starts a new one."

**Test all in ENV** appears below the list when the filter is one environment where you hold
`MANAGE_CONNECTION`; with **All environments** the screen says "Choose an environment to test all
of its connections." It asks for a test of every connection of that environment (one that already
has a test open is not tested again), then says "Testing 4 connections in dev…" and, when all are
done, "Tested 4 connections in dev: 3 OK, 1 not OK." The rows change as their tests finish.

### A connection's page

Open a connection by its name. The page shows:

- **Connection**: the kind with its one-line description, the address and the settings, each key
  with its value; `tls` and `auth` in words ("encrypted, certificate checked", "bearer token");
  the CA certificate as its SHA-256 fingerprints, never its text; the **Secret**'s name with "its
  value is never shown", or "none"; who created it and when, and when it was last changed.
- **Used by**: each asset that declares it as "repository: asset", or "No deployed asset declares
  it."
- **Last test**: the status with its full date; for a failure the cause and the next step from
  [the outcome table](#test-a-connection), in the screen's words ("Give the secret again: Edit the
  connection and enter new values."); for those who hold `MANAGE_CONNECTION` also the worker's
  message and "by NAME"; "Tested before the last change; test it again." when the test is older
  than the connection; the run that failed on it, if any, with its full date, linked to the task,
  and "silver.customers failed reading it; open the task to read why."; and the
  `lakana connection test` command to copy.

**Test**, **Edit** and **Delete…** are at the top, each only where you may use it, and **Test**
is the primary action. An address that names no connection shows "This page does not exist"
and a link back to the list.

### A run that failed on a connection

A test says whether a worker can reach the source; a run says whether the asset could read it. A
connection shows the newest task that failed on it when all of these hold:

- the asset's code ended with an error of a `lakana.ingest` helper (`connections.get`,
  `files.read`, `files.list`, `rest.read`, `sql.read`) called with this connection. Each such
  error carries the connection in `IngestError.connection`, and the run records it. An error of
  your own client (after `connections.get`, [Anything else: your own client](../how-to/load-data-from-a-source.md#anything-else-your-own-client))
  names no connection, and neither does `ConnectionNotDeclared` nor a `UsageError` for a mistake
  in the asset's code (an empty query, a `params=` or `paginate=` that does not fit). A
  `ConnectionSetupError` does name it: a connection of the wrong kind for the helper, a missing
  secret key, a token endpoint or CA certificate that does not work, where fixing the connection
  is the fix ([the errors](ingest.md));
- the asset declares the connection: the worker keeps only a declared name;
- the task failed after the connection's last change: a change may be the fix, and a connection
  deleted and made again under the same name is a new one. New values for its secret count as a
  change, whether given with `lakana connection update --password-stdin` (or `--values-stdin`)
  or with `lakana secret rotate`;
- it is still the asset's newest ending: a later run of the same asset (same repository and
  environment) that succeeded, or failed for another reason, hides it, and so does a success that
  ended at the same moment;
- you may read the environment's runs (`RUN` or `READ_ENVIRONMENT`, as for the run's own page).

### Add a connection

**Add connection** opens a form in this order:

1. **Environment**, among those where you may add a connection; it starts at the list's filter
   when that is one of them. Without a right in any environment the page says "You may not add
   connections in any environment. Ask an administrator for MANAGE_CONNECTION." and shows no form.
2. **Kind**: the nine kinds, each with a line, which is the list of what Lakana can read from
   ([Kinds](#kinds)). Choosing another kind starts its fields afresh.
3. **Name**: lower-case letters, digits and `-`, starting with a letter, at most 63 characters
   (the screen checks it when you leave the field; the server's answer is the last word).
4. **The kind's fields**, the address first and then the settings, named as the command line names
   them (`host`, `port`, `database`, `user`, `tls`, …). A **port** left empty is the kind's
   default, shown in grey; an `s3` **endpoint** left empty is the region's. `ca_certificate` is a
   PEM file (`.pem`, `.crt` or `.cer`, at most 64 KiB) read in your browser; the form shows only
   that it was read. An `http` connection shows the fields of its `auth`.
5. **Secret**, for the kinds that take one: **New values**, one field for each key of the secret
   (a password, a token, an access key, a service account key that can be read from a file, an
   Azure account key or SAS token, or 0 to 16 keys of your own names for `tcp`); or, with
   `MANAGE_SECRET`, **An existing secret of ENV** and its name ([Who may do what](#who-may-do-what)).

**Save** asks for a passkey touch when the server needs a fresh one (within the last 15 minutes),
then creates the connection and opens its page, which says "Created NAME. Test it now to see that
a worker can reach it." with **Test now** as its primary action.
Someone who signs in through the organisation's identity provider confirms there
instead, then presses the action again ([single sign-on](../how-to/bring-your-team-onto-single-sign-on.md#what-linking-changes-for-a-person)). A refusal, such as a name taken,
shows above the form in the server's words and keeps what you typed.

A secret's values live only in the form. Each is a password field that your browser neither shows
nor remembers, it is sent only in the create request (again, unchanged, if a passkey touch is asked for first), it is never put in the
address, in storage or in an error, and the form forgets it when you save. No page shows it again.

### Edit a connection

**Edit** shows the environment, kind and name as text, because they never change, and starts from
the connection's fields with no secret value in them. The CA certificate shows its fingerprints,
with a file to replace it and **Remove the CA certificate**. **Secret** offers:

- **Keep NAME**, the default for a connection that has a secret;
- **New values**;
- **Another secret of ENV**, with `MANAGE_SECRET`;
- **Stop using a secret**.

A change that could send the secret somewhere new moves it: a new address, a weaker `tls`,
another CA certificate, another `auth` or `api_key_in`, another `token_url`
([Who may do what](#who-may-do-what)). When you make such a change, **Keep** gives way to **New
values** and the form asks for them. Without `MANAGE_SECRET` the page says so before you change
anything: "You may not manage secrets in ENV. A change of the address, TLS, CA certificate, or how
the secret is sent, needs the secret's values again." and **Keep** is disabled once the change
moves the secret, with the reason beside it: "This change could send NAME somewhere new; give its
values again." With `MANAGE_SECRET`, **Keep** stays allowed and says "NAME will be sent to the
new address."

**Save** sends only what changed (a setting you removed is cleared), asks for a passkey touch when
needed, and opens the connection's page with "Saved NAME." The last test is then older than the
connection, so the page says so. With nothing changed it says "Nothing to save." and sends
nothing. Without `MANAGE_CONNECTION` the address of Edit shows "You may not edit connections in
ENV. Ask an administrator for MANAGE_CONNECTION there." and a link back, with no form.

### Delete a connection

**Delete…** asks "Delete the connection NAME in ENV?" and says that its secret stays in the
environment, unbound (or "It has no secret."); **Delete** confirms. Delete needs no passkey
touch. After it the list opens with "Deleted NAME."

While an asset declares the connection, the dialog says "NAME is still in use", lists each
"repository: asset", and tells you to remove it from the asset's `@asset(connections=[...])` in
git and deploy, then delete it. It only offers **Close** and sends nothing. A connection that an
asset declared meanwhile is refused by the server, and the dialog shows its sentence.

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
one leading UTF-8 byte order mark and one trailing line break (`\n`, `\r\n` or `\r`, as a Windows
editor writes them), under the kind's single key (`password`, `token`, `api_key`,
`client_secret`, `secret_access_key`, `service_account_json` or `account_key`); `--values-stdin`
reads a JSON object of several keys, such as `{"secret_access_key": "...", "session_token":
"..."}`, and drops a leading byte order mark too.

List and show:

<!-- source: docs/examples/connections/run.sh#list -->
```bash
lakana connection list --env dev
```

<!-- output: connections#list -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ name    ┃ kind       ┃ address                                    ┃ secret            ┃ declared_by ┃ last_test    ┃
┡━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ exports │ directory  │ root=shared path=exports                   │                   │ (none)      │ never tested │
│ crm-db  │ postgresql │ host=db.example.com port=5432 database=crm │ crm-db-connection │ (none)      │ never tested │
└─────────┴────────────┴────────────────────────────────────────────┴───────────────────┴─────────────┴──────────────┘
```

`--all-environments` lists the connections of every environment you may read, newest first over
all of them, with an `environment` column after the name; give it without `--env`:

<!-- source: docs/examples/connections/run.sh#list-all -->
```bash
lakana connection list --all-environments
```

<!-- output: connections#list-all -->
```text
┏━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ name    ┃ environment ┃ kind       ┃ address                                    ┃ secret            ┃ last_test    ┃
┡━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ exports │ dev         │ directory  │ root=shared path=exports                   │                   │ never tested │
│ crm-db  │ dev         │ postgresql │ host=db.example.com port=5432 database=crm │ crm-db-connection │ never tested │
└─────────┴─────────────┴────────────┴────────────────────────────────────────────┴───────────────────┴──────────────┘
Not shown: declared_by. Use --output json to see it.
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
Last test:   never tested
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

`list` has a `last_test` column and `show` a `Last test:` line: `never tested`, `queued`,
`running`, or the outcome and when it finished, followed by `(before the last change)` when the
connection changed after it; `(server does not say)` from a server older than this CLI, which sends
no last test. When a run failed on the connection ([A run that failed on a
connection](#a-run-that-failed-on-a-connection)), `show` adds a line under it:
`Last run:    failed on it at 2026-11-18T09:30:00+00:00: silver.customers in run RUN_ID (lakana
runs show RUN_ID --env prod)`. With `MANAGE_CONNECTION`, an open test also says when it was
requested (`queued at 2026-11-17T08:00:00+00:00`), and `show` adds who ran a test and its message
(`by dana: password authentication failed ...`), and for an open test its id
(`queued at ... (--id ID)`), which `lakana connection test NAME --id ID` shows. For an outcome other than `ok`, `show` prints
what to do next on standard error.

`--output json` prints the record of the REST answer (below) for `show`, `create` and `update`.

## REST

| Route | Right | Answer |
|---|---|---|
| `POST /api/v1/environments/{env}/connections` | `MANAGE_CONNECTION` | 201 and the connection, whose `secret` is the name its new secret got; 409 when the name is taken; 422 naming the field that is not valid |
| `GET /api/v1/environments/{env}/connections?limit=&cursor=` | `READ_ENVIRONMENT` | `{"items": [...], "next_cursor": ...}`, newest first, `limit` from 1 to 200 (default 100) |
| `GET /api/v1/connections?environment=&limit=&cursor=` | `READ_ENVIRONMENT` in each environment listed | the same body over every environment where you hold it, newest first over all of them; an environment without it is left out, never a 403 or 404. With `environment`, exactly the list of that environment, with its 403 and 404. Its `next_cursor` pages this list only: a cursor of the list of one environment is a 422 |
| `GET /api/v1/connections/environments` | `READ_ENVIRONMENT` in each environment listed | `[{"name", "actions"}]`, sorted by name: every environment where you hold it. `actions` holds `create` and `test` with `MANAGE_CONNECTION` there, and `name_secret` (naming an existing secret for a connection) with `MANAGE_SECRET` too; `[]` for a reader |
| `GET /api/v1/environments/{env}/connections/{name}` | `READ_ENVIRONMENT` | the connection; 404 names no other connection |
| `PATCH /api/v1/environments/{env}/connections/{name}` | `MANAGE_CONNECTION`, and `MANAGE_SECRET` as above | the connection |
| `DELETE /api/v1/environments/{env}/connections/{name}` | `MANAGE_CONNECTION` | 204; 409 while a deployed project version declares it |
| `POST /api/v1/environments/{env}/connections/{name}/tests` | `MANAGE_CONNECTION` | 202 and the test `{"id", "state", "outcome", "message", "created_at", "finished_at"}` (`state` is `queued`, `running` or `done`; `outcome` and `message` are `null` until done); 409 (naming the open test's id) while a test of the connection is queued or running and less than 10 minutes old; an older one is finished as `failed` first |
| `POST /api/v1/environments/{env}/connection-tests?limit=&cursor=` | `MANAGE_CONNECTION` | 202 and `{"items": [{"connection", "test_id", "queued"}], "next_cursor"}`: Test all of one page of the environment's connections, newest first (`limit` 1 to 200, default 200; the list's cursor). A connection with an open test under 10 minutes old gets that test with `queued: false`, never a 409; an older one is finished as `failed` first; every other connection gets a new test with `queued: true`. 404 for an unknown environment; 409 when a connection changed at the same time: the connections before it stay queued, and sending the request again returns their tests with `queued: false`; 422 for a cursor of another list |
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
list of `{"repository": ..., "asset": ...}`), `created_by` (a principal's name), `created_at`,
`updated_at`, `environment` (its environment's name), `last_test`, `actions` and
`last_run_failure`. No answer of
these routes holds a secret's value; only the task route above does, for the running task itself.

`last_test` is the connection's newest test of any state, or `null` if it was never tested:

- every reader gets `state` (`queued`, `running` or `done`), `outcome` (`null` until done) and
  `finished_at` (`null` until done);
- a holder of `MANAGE_CONNECTION` also gets `id`, `created_at`, `message` and `requested_by` (the
  name of who asked, `null` when that principal is gone). For anyone else these four keys are
  absent, not `null`: the message can name paths, hosts and ports the worker reached, while each
  outcome's cause and next step are public ([Test a connection](#test-a-connection)).

A change keeps `last_test`: when its `finished_at` is before `updated_at`, it tested the connection
as it was before the change.

`last_run_failure` is `{"run_id", "asset", "finished_at"}`: the newest task that failed on the
connection, under the rules of [A run that failed on a
connection](#a-run-that-failed-on-a-connection), or `null`. A caller who may not read the
environment's runs gets `null`.

`actions` says what your rights allow on the connection, in this order: `test`, `update` and
`delete` with `MANAGE_CONNECTION` on the environment, and `name_secret` with `MANAGE_SECRET` there
too (naming a secret not bound to the connection, or a change that could send its secret somewhere
new without new values). A reader gets `[]`. The server still decides every request: an offered
action can be refused, for example a delete while a deployed version declares the connection.

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
for each in client.connections.iter_all():  # every environment you may read
    print(each.environment, each.name)
client.connections.delete("prod", "crm-db")
client.secrets.bind("prod", "crm-reader", "connection", "crm-db")
events = client.audit_events.list(kind="connection")
```

`update` takes `address`, `settings`, `secret`, `secret_values` and `clear` (a list of names).
`list_all` (one page, `environment=`, `limit=`, `cursor=`) and `iter_all` list the connections of
every environment you may read, as `GET /api/v1/connections` does; `client.connections.environments()`
returns those environments as `ConnectionEnvironment`s with `name` and `actions`.
A `Connection` also has `environment`, `last_test` (a `LastConnectionTest` with `state`,
`outcome` and `finished_at`, and `id`, `created_at`, `message` and `requested_by`, which are
`None` without `MANAGE_CONNECTION`), `actions`, a tuple of the words of `actions` above, and
`last_run_failure` (a `ConnectionRunFailure` with `run_id`, `asset` and `finished_at`, or `None`).

`client.connections.test("prod", "crm-db")` queues a test and polls it (0.5 s, growing to 2 s) until
it is done or `timeout` (120 s) passes, and returns a `ConnectionTest` with `id`, `state`,
`outcome`, `message`, `created_at` and `finished_at`. `wait=False` returns it as queued;
`test_id=` looks at an earlier test. A test nobody finished in time raises `TimeoutError`, naming
the state, the worker log to read and the command that looks again.

`client.connections.test_all("prod")` tests every connection of the environment: it queues a test
of each, page by page, then polls them as `test` does until every one is done, and returns a
`dict` of each connection's name to its `ConnectionTest`. A connection with an open test gets that
test. `wait=False` reads each test once; a wait that runs out raises `TimeoutError` naming every
connection still open. `queue_tests("prod", limit=, cursor=)` is one page of it, as
`POST .../connection-tests` answers: a `QueuedConnectionTestPage` of `QueuedConnectionTest`s
(`connection`, `test_id`, `queued`) and `next_cursor`.

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
  registers it. `files.read` and `rest.read` register what they derive: the basic-auth header,
  each OAuth2 access token and the egress proxy's credential.
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

Files are read with [`files.read`](ingest.md#filesread), which returns them as a table, from
`s3`, `gcs`, `azure`, `http` and `directory` connections. It gives `proxy_url` and the trust
bundle to its own client, and never follows a symbolic link out of a directory connection's
root. Databases are read with [`sql.read`](ingest.md#sqlread), from `postgresql`, `mariadb` and
`sqlite-file` connections: it dials `relay_address` under the Docker runner, runs every query
read-only, and sends MariaDB only a statement that starts with `SELECT`, `WITH`, `VALUES` or
`TABLE` (MariaDB commits before DDL, which no read-only transaction stops). JSON REST APIs are
read with [`rest.read`](ingest.md#restread), from `http` connections, page by page, never
sending the credential to another host than the base URL's. A `tcp` connection has no helper; use `connections.get` with your own client, as the recipe
[Anything else: your own client](../how-to/load-data-from-a-source.md#anything-else-your-own-client)
does, and for SQL Server the recipe [Read from a SQL Server database](../how-to/read-from-a-sql-server-database.md).

### What a task reaches, and how

When an attempt starts, the worker reads the connections the asset declares and gives the task
exactly their endpoints, nothing more. Under the Docker runner (`LAKANA_RUN__RUNNER=docker`) the
task has no other way out: HTTPS goes through the egress proxy with a token for those endpoints
only, databases and `tcp` through a relay of its own, and directories are read-only mounts. The
local runner connects directly.

| Kind | Docker runner | Local runner |
|---|---|---|
| `http`, `s3`, `gcs`, `azure` | `proxy_url` is the egress proxy; give it to your HTTPS client (`files.read` does). It holds the task's proxy token, so it is left out of the connection's `repr` and shown as `***` in the task's log. `relay_address` and `root_path` are `None`. | Every field is `None`: connect to `address` directly. |
| `postgresql`, `mariadb`, `tcp` | `relay_address` is `lakana-relay:<port>` (ports from 15001, in the order the asset declares its connections); connect there instead of `address["host"]`. The helpers' TLS still checks the real host name; your own client of a `tcp` connection checks it only if it takes the host name apart from the address it dials ([the SQL Server recipe](../how-to/read-from-a-sql-server-database.md#tls) shows one). | `relay_address` is `None`: connect to `address` directly. |
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
source in its lineage. `lakana table lineage` prints it with its kind, such as
`connection crm-db (postgresql)`, and as `connection crm-db (deleted)` once the connection is
gone. The SQL recipe of [Load data from a source](../how-to/load-data-from-a-source.md#a-sql-database)
reads a SQLite file:

<!-- source: docs/examples/ingest-sql/run.sh#lineage -->
```bash
lakana table lineage bronze.orders --env dev
```

<!-- output: ingest-sql#lineage -->
```text
bronze.orders
└── connection source-db (sqlite-file)
```

In the REST answer and the SDK the node has `kind` `CONNECTION`, the connection's name and id,
and its kind in `connection_kind`.

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
