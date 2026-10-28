# What leaves the machine

This page lists the outbound network connections we found in Lakana's code, who starts each
and how to change it. It also says how we checked, and what we could not check. If you run Lakana
where network access is controlled, start here.

**Short answer.** We found no telemetry and no phone-home: the code of the four Lakana
packages contains no usage reporting, no update check and no connection to a project server.
That statement is about the code we ship and the Python libraries it uses. For the
programs it starts and the container images of the Compose bundle, see "What we could not
verify".

## The connections Lakana makes

Each connection in the first two groups is one you configure. The third group, and the
cloud credential lookup under "What we could not verify", are not.

**Between the parts of an instance, to places you configure**

| From | To | Set by |
|---|---|---|
| The `lakana` CLI and SDK, and the setup job of the Compose bundle | The Lakana server of the profile (or setup URL) you use, through a proxy if the usual proxy variables of your environment name one. In local mode, also the server it started on this machine (`/healthz` on the loopback address, never through a proxy). | The profile's URL |
| Server, worker and migration job | The Postgres catalog database. The worker takes its work from the database, not from the server. In local mode the catalog is a SQLite file on this machine and no connection is made. | `LAKANA_DATABASE__URL` |
| Server and worker | The object storage of each environment (an S3-compatible endpoint, or local files), with that environment's credentials. The worker writes query results, the code archive of each deploy and the logs of task runs there, and reads table files. The server stores and serves the files of artifacts there, and the worker's sweep removes the ones nothing needs. | The environment's storage settings |
| Worker (DuckDB) | The object storage, to read table files through presigned URLs that the worker creates. Each query process opens only the locations of the tables that query reads (in local mode, their directories), plus its own temporary directory and the file of trusted certificate authorities; any other address or file is refused. One exception: if the store answers with a redirect, DuckDB follows it. | The environment's storage settings. Trusted certificate authorities: `AWS_CA_BUNDLE`, else `SSL_CERT_FILE`, else the system store, else the `certifi` bundle (see [Network settings](../reference/network-settings.md#object-store-certificates-for-queries)) |
| Python tasks the worker starts | The Lakana server, through the SDK (REST) | `LAKANA_RUN__SERVER_URL` |

**Connections to your own services and package sources**

| From | To | Set by |
|---|---|---|
| Worker (`git`) | The git remotes you registered, for `ls-remote` and `fetch`, over HTTPS or SSH. Nothing else: submodules, credential helpers and other protocols are off. | The repository's URL. For HTTPS: `LAKANA_GIT__HTTPS_PROXY`, `LAKANA_GIT__NO_PROXY`, `LAKANA_GIT__CA_BUNDLE` (see "Git network settings" in [Projects](../how-to/projects.md)) |
| Worker (`uv`) | A package index, when it builds the environment of a project with Python assets: the default index (PyPI) and the files the project's `uv.lock` names, plus an extra index if you set one, plus each registered package index a project's lock names, with that index's credential from the environment's secret. The build connects to the hosts the lock names, follows the redirects those hosts send, and goes through the operator's proxy when one is set. A build without a credential may build a source distribution, whose build requirements can come from the index and whose build backend has network access; a build with a registered index's credential installs wheels only. It never downloads a Python interpreter. | `LAKANA_RUN__INDEX_URL` (an extra index), `lakana index add` (registered indexes), `LAKANA_RUN__HTTPS_PROXY`, `LAKANA_RUN__NO_PROXY`, `LAKANA_RUN__CA_BUNDLE` |
| Server (CI OIDC) | The CI providers in `LAKANA_CI_OIDC__ISSUERS` (GitHub Actions, GitLab.com or your own GitLab), only when a CI job exchanges an ID token: the issuer's discovery document (`/.well-known/openid-configuration`) and its public signing keys, over `https` on port 443, at most once an hour per issuer (once a minute for an unknown key), from public addresses only unless the host is in `LAKANA_CI_OIDC__ALLOW_HOSTS`, with no redirect followed and at most 64 KiB read. Nothing about the job or the instance is sent. Off while `LAKANA_CI_OIDC__ISSUERS` is empty (the default). | `LAKANA_CI_OIDC__ISSUERS`, `LAKANA_CI_OIDC__ALLOW_HOSTS`, `LAKANA_CI_OIDC__PROXY`, `LAKANA_CI_OIDC__CA_BUNDLE` (see [CI trust rules and CI tokens](../reference/ci-trust-rules.md)) |
| Python tasks, through the `egress` proxy and a relay of their own (docker runner) | The endpoints of the connections the task's asset declares, and nothing else: HTTPS to an `http`, `s3`, `gcs` or `azure` connection's host and port (and an `oauth2` connection's token endpoint, a `gcs` key's token endpoint), a database or `tcp` connection's host and port through the task's relay. Never the Lakana stack, another task, the cloud metadata address, the Docker host's loopback, gateways or names, or a range of `LAKANA_EGRESS_REFUSED`. | The connections (`lakana connection create`) and the asset's `connections`. `LAKANA_EGRESS_UPSTREAM_PROXY`, `LAKANA_EGRESS_REFUSED` (see [How a task reaches its connections](../reference/network-settings.md#how-a-task-reaches-its-connections)) |
| Worker (`lakana connection test`) | The endpoints of the connection under test, and nothing else: with the docker runner from a container of the task image under the same rules as a task with that connection; with the local runner from the worker itself. It resolves the host, connects, checks TLS and, where the kind has one, logs in once (a database's `SELECT 1` in a read-only transaction, one `GET` of an `http` connection's base URL, a list of at most one object). | The connection; started by `lakana connection test` (`MANAGE_CONNECTION`) |
| Worker (webhooks) | The webhook URL a job's `notify:` names, as a secret of the environment: only a public `https` address on port 443 (or a host the operator allowed, never a loopback, link-local or cloud metadata address), checked before it connects, with no redirect followed, and a message that carries job, run and check names only. | The secret. `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY`, `LAKANA_WEBHOOK__CA_BUNDLE` (see "Webhook network settings" in [Projects](../how-to/projects.md)); `LAKANA_WORKER__NOTIFICATIONS=false` stops a worker from sending |

**One connection to the DuckDB project**

| From | To | Set by |
|---|---|---|
| Worker (DuckDB) | DuckDB's extension repository, `extensions.duckdb.org`, to download the `httpfs` extension the first time a worker opens a connection on a machine that does not have it yet; if that server answers with a server error (HTTP 5xx), DuckDB asks its backup, `extensions.duckdb-backup.org`, for the same file. One download of one file; no query data is sent. | Not configurable in Lakana (see below) |

The container image already contains the extension, because the image installs it when it is
built, so a worker that runs from the image makes no such download. A worker installed with
`pip` makes it once, on its first query. Each DuckDB release has its own build of the
extension, so after an upgrade to a new DuckDB release (Lakana runs DuckDB 2.0) a `pip`
worker downloads it once more.

A few more things you may see:

- **Your own Python assets** reach only the endpoints of the connections they declare when they
  run in containers (the Compose bundle's default, see the table above). With the local runner
  and in local mode they run as ordinary processes on the worker and can make any connection
  their code asks for; Lakana does not restrict that. See "Python assets" in
  [Projects](../how-to/projects.md).
- **Docker** pulls the images of the Compose bundle (the Lakana image, Postgres and SeaweedFS)
  from their registries when you run `docker compose up`. Docker starts that, not Lakana.
- **Job notifications** go to the webhook a job's `notify:` names, and only there. A message
  carries the names of the job, the environment, the repository, the commit, the run, the failed
  tasks and the failed checks, the time, the status and a delivery id. It never carries table
  data, a secret, or the text of an error message. The address is a secret of the environment; it
  is never written to a log, the audit log or an answer. See
  [Get told when a job fails](../how-to/get-told-when-a-job-fails.md).
- **Not built yet.** There is no OIDC login for people in the code today. When it arrives it
  will make connections to the provider you configure, and this page will list them. (The CI
  OIDC exchange above is for CI jobs only.)
- **There is no web interface** in the repository today; it comes before the public beta.
- **This documentation site** loads no font, script, image or style sheet from another host, so
  it also works on an internal network.

## What is stored where

- **The catalog database (Postgres):** environments, namespaces and table records, principals,
  grants, row filters and column masks, the audit log, deploy and run records, and secrets,
  which are stored encrypted. No API route returns a stored secret's value.
- **Object storage:** the table data and Iceberg metadata, query results, the code archive of
  each deploy and the logs of task runs.

## How we checked

We read and searched the source of the four Lakana packages (`packages/*/src`) and the
libraries in the lock file, and ran two small tests against DuckDB 1.5.5 and again against
DuckDB 2.0.0, the version in the lock file.

- **Our code.** We searched for HTTP clients, sockets, `urllib`, subprocesses, update checks,
  usage reporting and the host names of analytics services. The only HTTP client is `httpx`: in
  the SDK, which talks to the URL of the profile (and, with proxy settings from the
  environment ignored, to the loopback address in local mode), and in `lakana_core.net`, which the
  worker uses for webhooks and the server for the CI providers' keys, both listed above. The only other request is the
  container's health check, to `127.0.0.1`. The only programs the code starts are `git`,
  `uv`, the task's own Python interpreter, and Lakana's own server and worker.
- **Python libraries.** We searched the installed source of the libraries Lakana depends on
  (`pyiceberg`, `fsspec`, `s3fs`, `aiobotocore`, `fastapi`, `starlette`, `uvicorn`,
  `pydantic`, `sqlalchemy`, `alembic`, `pg8000`, `sqlglot`, `httpx`, `requests` and others)
  for telemetry, analytics, update-check and error-reporting code. We found none.
- **DuckDB.** It has no setting for telemetry. Its extension settings are listed above: the
  worker turns off automatic installation and loading of extensions and refuses community
  extensions, so the `httpfs` download is the only extension traffic. We checked that
  `INSTALL` contacts the extension repository when the extension is missing, and does not when
  it is already installed.
- **Where the extension download goes.** The repository setting is empty by default, so
  the host is DuckDB's own default. We read it in DuckDB 2.0.0's source
  (`extension_install_info.hpp`): `http://extensions.duckdb.org`, with
  `http://extensions.duckdb-backup.org` tried only when the first answers with a server error.
  On a machine without network access, `INSTALL httpfs` failed naming
  `http://extensions.duckdb.org/v2.0.0/linux_amd64/httpfs.duckdb_extension.gz`. We did not
  watch the request on the wire.

## What we could not verify

- **Compiled programs and libraries.** We searched Python source only. We did not inspect the
  machine code of DuckDB, PyArrow, `pydantic-core`, `cryptography` and `uv`. For DuckDB we
  looked at its settings and one behaviour, as above. For `uv`, which the worker runs to
  build environments, we know only from the code that it is run to install packages from an index. We did
  not check what else it does.
- **Offline extension install.** Lakana has no setting to point DuckDB at a mirror or to
  switch the download off. On a machine without internet access, put the extension in
  DuckDB's extension directory before the first query, or use the container image.
- **Cloud credential lookup.** The S3 libraries Lakana uses can look for credentials at the cloud
  instance metadata address (`169.254.169.254`, in `botocore`) when an S3 environment is
  configured without access keys. We did not test when this happens in Lakana. Set
  `AWS_EC2_METADATA_DISABLED=true` to turn it off.
- **Postgres and SeaweedFS images.** We did not check the images the Compose bundle starts for
  telemetry of their own. Check their documentation if it matters to you.
- **Other versions.** This page describes the code in this repository at the time it was
  written. A later version may behave differently.
