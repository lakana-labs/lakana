# What leaves the machine

This page lists the outbound network connections we found in Lakana's code, who starts each
and how to change it. It also says how we checked, and what we could not check. If you run Lakana
where network access is controlled, start here.

**Short answer.** We found no telemetry and no phone-home: the code of the four Lakana
packages contains no usage reporting, no update check and no connection to a project server.
That statement is about the code we ship and the Python libraries it uses. For the
programs it starts and the container images of the Compose bundle, see "What we could not
verify". To run Lakana with no internet access at all, see
[Install without internet access](../how-to/install-without-internet-access.md).

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
| `lakana init --template python` (the CLI, on your machine) | A package index, once, to run `uv lock` for the new project: the public index unless your `uv` is configured otherwise (`UV_DEFAULT_INDEX`, `UV_FIND_LINKS`, a proxy in `HTTPS_PROXY`). The command stops with a message when it cannot connect. Without `--template` it makes no such request. | Do not use `--template python`; or configure `uv` on that machine |
| The `lakana` CLI and SDK in a GitHub Actions job | The runner's ID token service, at the address in `ACTIONS_ID_TOKEN_REQUEST_URL` with the runner's own request token, to get an ID token for the audience of your Lakana server, only when the job signs in by OIDC. A GitLab job reads its token from an environment variable and makes no request. | The CI file you choose (`--ci-auth`, see [Deploy from GitHub Actions](../how-to/deploy-from-github-actions.md)) |
| Worker (`uv`) | A package index, when it builds the environment of a project with Python assets: the default index (PyPI) and the files the project's `uv.lock` names, plus an extra index if you set one, plus each registered package index a project's lock names, with that index's credential from the environment's secret. The build connects to the hosts the lock names, follows the redirects those hosts send, and goes through the operator's proxy when one is set. A build without a credential may build a source distribution, whose build requirements can come from the index and whose build backend has network access; a build with a registered index's credential installs wheels only. It never downloads a Python interpreter. | `LAKANA_RUN__INDEX_URL` (an extra index), `lakana index add` (registered indexes), `LAKANA_RUN__HTTPS_PROXY`, `LAKANA_RUN__NO_PROXY`, `LAKANA_RUN__CA_BUNDLE` |
| Server (CI OIDC) | The CI providers in `LAKANA_CI_OIDC__ISSUERS` (GitHub Actions, GitLab.com or your own GitLab), only when a CI job exchanges an ID token: the issuer's discovery document (`/.well-known/openid-configuration`) and its public signing keys, over `https` on port 443, at most once an hour per issuer (once a minute for an unknown key), from public addresses only unless the host is in `LAKANA_CI_OIDC__ALLOW_HOSTS`, with no redirect followed and at most 64 KiB read. Nothing about the job or the instance is sent. Off while `LAKANA_CI_OIDC__ISSUERS` is empty (the default). | `LAKANA_CI_OIDC__ISSUERS`, `LAKANA_CI_OIDC__ALLOW_HOSTS`, `LAKANA_CI_OIDC__PROXY`, `LAKANA_CI_OIDC__CA_BUNDLE` (see [CI trust rules and CI tokens](../reference/ci-trust-rules.md)) |
| Python tasks, through the `egress` proxy and a relay of their own (docker runner) | The endpoints of the connections the task's asset declares, and nothing else: HTTPS to an `http`, `s3`, `gcs` or `azure` connection's host and port (and an `oauth2` connection's token endpoint, a `gcs` key's token endpoint), a database or `tcp` connection's host and port through the task's relay. Never the Lakana stack, another task, the cloud metadata address, the Docker host's loopback, gateways or names, or a range of `LAKANA_EGRESS_REFUSED`. | The connections (`lakana connection create`) and the asset's `connections`. `LAKANA_EGRESS_UPSTREAM_PROXY`, `LAKANA_EGRESS_REFUSED` (see [How a task reaches its connections](../reference/network-settings.md#how-a-task-reaches-its-connections)) |
| Worker (`lakana connection test`) | The endpoints of the connection under test, and nothing else: with the docker runner from a container of the task image under the same rules as a task with that connection; with the local runner from the worker itself, to whatever address the connection names, internal addresses included. It resolves the host, connects, checks TLS and, where the kind has one, logs in with the connection's secret and sends authenticated requests (a database login and `SELECT 1` in a read-only transaction, one `GET` of an `http` connection's base URL with its auth, an OAuth2 or GCS token request, a list of at most one object). | The connection; started by `lakana connection test` (`MANAGE_CONNECTION`) |
| Worker (webhooks) | The webhook URL a job's `notify:` names, as a secret of the environment: only a public `https` address on port 443 (or a host the operator allowed, never a loopback, link-local or cloud metadata address), checked before it connects, with no redirect followed, and a message that carries job, run and check names only. | The secret. `LAKANA_WEBHOOK__ALLOW_HOSTS`, `LAKANA_WEBHOOK__PROXY`, `LAKANA_WEBHOOK__CA_BUNDLE` (see "Webhook network settings" in [Projects](../how-to/projects.md)); `LAKANA_WORKER__NOTIFICATIONS=false` stops a worker from sending |

**One connection to the DuckDB project**

| From | To | Set by |
|---|---|---|
| Worker (DuckDB) | DuckDB's extension repository, `extensions.duckdb.org`, to download the `httpfs` extension the first time a worker opens a connection on a machine that does not have it yet; if that server answers with a server error (HTTP 5xx), DuckDB asks its backup, `extensions.duckdb-backup.org`, for the same file. One download of one file; no query data is sent. | `LAKANA_WORKER__EXTENSION_SOURCE=none` never downloads (see below) |

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
- **Task images.** With the docker runner, the launcher asks the Docker daemon of the host to pull
  a task image (`lakana-task:<tag>-py3.X`) from its registry the first time a task or build needs
  it and the host does not have it. Lakana starts that request; the daemon makes the connection.
  Load the images you need beforehand, or point `LAKANA_TASK_IMAGE` at an internal registry (see
  [Install without internet access](../how-to/install-without-internet-access.md#the-container-images)).
- **Job notifications** go to the webhook a job's `notify:` names, and only there. A message
  carries the names of the job, the environment, the repository, the commit, the run, the failed
  tasks and the failed checks, the time, the status and a delivery id. It never carries table
  data, a secret, or the text of an error message. The address is a secret of the environment; it
  is never written to a log, the audit log or an answer. See
  [Get told when a job fails](../how-to/get-told-when-a-job-fails.md).
- **No OIDC login for people.** The code has none today; the CI OIDC exchange above is for CI
  jobs only.
- **There is no web interface** in the repository today.
- **This documentation site** loads no font, script, image or style sheet from another host, so
  it also works on an internal network.

## Without storage keys: the cloud metadata address

If an environment's storage is an `s3://` root and its options and secret hold no access key
(`s3.access-key-id`), Lakana gives the S3 libraries no credentials, and they look for some the
way the AWS tools do: environment variables, configuration files and, last, the instance
metadata service of a cloud machine at `169.254.169.254`. The server and the worker both make
storage calls (the server's readiness check, for one, is a metadata request to each environment's
storage), so either may ask. An environment with keys, which is what the Compose bundle creates, never does, and neither
does a local `file://` root. Lakana uses two S3 clients, and we checked both: `s3fs` (through
`botocore`) and `pyarrow` (the AWS C++ library, through `pyiceberg`). With no keys, each sent
requests to the metadata endpoint (we pointed `AWS_EC2_METADATA_SERVICE_ENDPOINT` at a listener
on this machine to see them), and with `AWS_EC2_METADATA_DISABLED=true` in the environment, each
sent none. Set the variable on the `server` and `worker` services if your environments have no
keys and the machine is not a cloud machine. On a cloud machine where an instance role is how the
storage is reached, do not set it: the role is that lookup.

## Libraries and programs with a telemetry switch

| Part | What we found | If you want to be sure |
|---|---|---|
| DuckDB 2.0.0 | No telemetry setting. The only traffic we know is the extension download above. Starting it and running `SELECT 42` made no network connection. | `LAKANA_WORKER__EXTENSION_SOURCE=none` |
| `pyiceberg`, `fsspec`, `s3fs`, `aiobotocore`, `botocore` | No telemetry or update check in their source. `botocore` and the AWS C++ library of `pyarrow` look for credentials as the section above says; nothing else. | `AWS_EC2_METADATA_DISABLED=true` |
| `pyarrow` | The installed wheel is built with OpenTelemetry support (`pyarrow.is_opentelemetry_enabled()` is `True`). We found no exporter that starts on its own: reading and writing a Parquet file in memory made no network connection. The library names an environment variable, `ARROW_TRACING_BACKEND`, for choosing an exporter; Lakana sets none. | Do not set `ARROW_TRACING_BACKEND` or `OTEL_*` variables on the server or the worker. |
| `httpx`, `fastapi`, `starlette`, `uvicorn`, `pydantic`, `sqlalchemy`, `alembic`, `pg8000`, `sqlglot`, `cryptography`, `typer`, `rich` | No telemetry in their source. | |
| `uv` | The worker runs it to build a project's environment, with `UV_PYTHON_DOWNLOADS=never`. We know of no telemetry setting of `uv`, and we did not inspect its binary. Its connections are the ones in the table above. | See "What we could not verify" |
| `pip` | Lakana never runs it, except that the image build installs the locked wheels with it. When you run `pip install` yourself, `pip` may ask the package index whether a newer `pip` exists. | `--disable-pip-version-check`, or `PIP_DISABLE_PIP_VERSION_CHECK=1` |

Importing the four Lakana packages and the libraries above made no network connection either: we
ran the import under `strace` and looked for `connect` calls to an IP address.

## What is stored where

- **The catalog database (Postgres):** environments, namespaces and table records, principals,
  grants, row filters and column masks, the audit log (the table `audit_events`: see
  [Export and keep the audit log](../how-to/export-and-keep-the-audit-log.md)), deploy and run
  records, and secrets, which are stored encrypted. No API route returns a stored secret's value.
  In local mode the catalog is a SQLite file in the instance's data directory
  (`~/.local/share/lakana/local`, or under `XDG_DATA_HOME`).
- **Object storage:** the table data and Iceberg metadata, query results, the code archive of
  each deploy, the files of artifacts and the logs of task runs.
- **The master key and the database password:** in the `.env` file of a Compose bundle. Whoever
  holds the master key and the database can read every stored secret.
- **The machines that run the server and the workers:** the process log (the output of each
  container, `docker compose logs`), whose alert lines are in [the log events reference](../reference/log-events.md); and on each
  worker its caches,
  the downloaded packages of uv and the Python environments of projects.
- **Where the CLI runs:** the profile file `~/.config/lakana/config.toml` (or under
  `XDG_CONFIG_HOME`) with the server's URL and a token. The CLI refuses to read it when other
  users can read it.

## How we checked

We read and searched the source of the four Lakana packages (`packages/*/src`) and the
libraries in the lock file, and ran two small tests against DuckDB 1.5.5 and again against
DuckDB 2.0.0, the version in the lock file.

- **Our code.** We listed every module of the four packages that imports a network client, with

  ```text
  git grep -n -E "^\s*(import|from) (httpx|http\.client|http\.server|urllib\.request|urllib3|requests|aiohttp|socket|socketserver|smtplib|obstore|botocore|aiobotocore|boto3|s3fs|gcsfs|adlfs|pyarrow\.fs|websockets|ftplib|xmlrpc|ssl|pymysql|adbc_driver_[a-z]+|pg8000|psycopg|paramiko|duckdb|uvicorn)\b" packages/*/src
  ```

  (test files excluded), and read each hit. The rest of this item is that result. `urllib` is
  imported only as `urllib.parse`, for parsing. No module imports `requests`, `aiohttp`,
  `urllib3`, `smtplib`, `websockets`, `botocore`, `boto3`, `s3fs`, `gcsfs`, `adlfs`, `ftplib`,
  `xmlrpc`, `paramiko` or `psycopg`.

  | Module | Connects to | Triggered by | Leaves the host? |
  |---|---|---|---|
  | `httpx` in `lakana.client`, `lakana.resources.*`, `lakana._http`, `lakana.errors` | The Lakana server of the profile | Any CLI or SDK call | Only if the server is on another host |
  | `httpx` in `lakana.cli.server` and `lakana.cli.local._supervisor` | `/healthz` of the local instance, on loopback, ignoring proxy variables | `lakana server start/stop --local` | No |
  | `httpx` in `lakana._ci_oidc` | The GitHub Actions ID token service, at the runner's `ACTIONS_ID_TOKEN_REQUEST_URL` | `lakana` in a GitHub Actions job that signs in by OIDC | Yes |
  | `httpx` in `lakana_core.net` (`https_json`, `egress`) | The CI providers' discovery and key documents (server); webhook addresses (worker) | A CI token exchange; a job's `notify:` | Yes |
  | `httpx` in `lakana_worker.egress` | The webhook address of a job's `notify:` | A job's notification | Yes |
  | `httpx` in `lakana.ingest._http`, `files`, `rest` | The `http` and `rest` connections' endpoints (and token endpoints) an asset declares | A Python asset that reads through them | Yes, to the source |
  | `obstore` in `lakana.ingest.files` and `_stores` | S3, GCS or Azure endpoints of the connections an asset declares; a compiled Rust HTTP client | A Python asset that reads files from them | Yes, to the source |
  | `adbc_driver_postgresql`, `adbc_driver_sqlite`, `pymysql` and `socket` in `lakana.ingest.sql`, `_check` | The database server of a connection an asset declares (SQLite: a local file); `lakana connection test` also opens a TCP connection | A Python asset that reads a database; `lakana connection test` | Yes, to the source |
  | `http.client` and `socket` in `lakana_worker.launcher.engine`; `socket` in `launcher_client`, `launcher.server` | The Docker Engine's Unix socket, and the launcher's own Unix socket | The launcher, with the docker runner | No (a Unix socket on the host) |
  | `socket` in `lakana_worker.launcher.egress` and `relay` | Outbound: the hosts and ports of a build's package index or of a task's declared connection, through the `egress` proxy and the relay; the relay listens on the task's network | A build or a task with the docker runner | Yes, to those hosts only |
  | `socket` in `lakana_core.net.egress` | Name resolution (`getaddrinfo`) and the address check before a webhook or CI request connects | The same requests | Resolver only |
  | `socket.gethostname` in `lakana_worker.__main__`, `healthcheck`; `socket` in `lakana.cli.server` | None: the worker names itself by host name; the CLI tests a local port is free | Start-up | No |
  | `uvicorn` in `lakana_server.__main__` | None outbound: it is the server that listens | `lakana-server` | No |
  | `duckdb` in `lakana_worker.query_child` | The presigned object-storage URLs of the tables a query reads, and the extension download above | A query | Yes if the storage is remote |
  | `fsspec` / `pyiceberg` in `lakana_core.storage`, `lakana_core.catalog` | The environment's object storage (`s3fs`, and `pyarrow` underneath) | Server and worker storage calls | Yes if the storage is remote |
  | `ssl` in `lakana_core.db`, `lakana.ingest.*` | None itself: certificate settings for the connections above | | |

  The Postgres catalog connection is made by `pg8000` through `sqlalchemy` from
  `LAKANA_DATABASE__URL`; no module of ours opens it directly. The container's health check is
  the `HEALTHCHECK` line of the `Dockerfile` (`urllib.request`, in a command, not in a package), to `127.0.0.1`. The only programs the code starts
  are `git`, `uv`, the task's own Python interpreter, and Lakana's own server and worker. We read
  the hits to say what each module connects to; we did not trace every call.
- **Python libraries.** We searched the installed source of the libraries Lakana depends on
  (`pyiceberg`, `fsspec`, `s3fs`, `aiobotocore`, `fastapi`, `starlette`, `uvicorn`,
  `pydantic`, `sqlalchemy`, `alembic`, `pg8000`, `sqlglot`, `httpx`, `requests` and others)
  for telemetry, analytics, update-check and error-reporting code. We found none.
- **The `lakana[assets]` extra.** It installs `polars`, `fastexcel`, `obstore`,
  `adbc-driver-manager`, `adbc-driver-postgresql`, `adbc-driver-sqlite` and `PyMySQL`, for projects
  with Python assets. We searched their installed Python source for telemetry, analytics and
  error-reporting code and found none. We did not inspect their compiled code (`polars`,
  `obstore` and the ADBC drivers are native). Each is used to read the source a user configures:
  `obstore` to an S3, GCS or Azure endpoint, the ADBC drivers and `PyMySQL` to a database server.
  We did not check whether the native parts open any other connection.
- **DuckDB.** It has no setting for telemetry. Its extension settings are listed above: the
  worker turns off automatic installation and loading of extensions and refuses community
  extensions, so the `httpfs` download is the only extension traffic. We checked that
  `INSTALL` contacts the extension repository when the extension is missing, and does not when
  it is already installed.
- **At run time.** We ran the import of the four packages and their libraries, a DuckDB query
  and a Parquet round trip in `pyarrow` under `strace`, and no `connect` call went to an IP
  address. We pointed `AWS_EC2_METADATA_SERVICE_ENDPOINT` at a listener on this machine and made
  each S3 client (`s3fs` and `pyiceberg`'s `pyarrow` one) call a bucket without keys, with and
  without `AWS_EC2_METADATA_DISABLED=true`: they asked the listener in the first case and not
  in the second.
- **Where the extension download goes.** The repository setting is empty by default, so
  the host is DuckDB's own default. We read it in DuckDB 2.0.0's source
  (`extension_install_info.hpp`): `http://extensions.duckdb.org`, with
  `http://extensions.duckdb-backup.org` tried only when the first answers with a server error.
  On a machine without network access, `INSTALL httpfs` failed naming
  `http://extensions.duckdb.org/v2.0.0/linux_amd64/httpfs.duckdb_extension.gz`. We did not
  watch the request on the wire.
- **Switching the download off.** `LAKANA_WORKER__EXTENSION_SOURCE` is `duckdb` by default,
  DuckDB's server as above, contacted only when the extension is not already in the worker's
  extension directory. With `none` the worker never downloads: at start it loads `httpfs` from
  `~/.duckdb/extensions` of the worker's user and refuses to start if it is not there, naming the
  exact file (for example `~/.duckdb/extensions/v2.0.0/linux_amd64/httpfs.duckdb_extension`, with
  the version and platform of that worker) and what to do: run the worker from the container
  image, which bundles the extension, or copy that file there. A mirror URL is not supported.
  Other extensions are never installed or loaded automatically.

## What we could not verify

- **Compiled programs and libraries.** We searched Python source only. We did not inspect the
  machine code of DuckDB, PyArrow, `pydantic-core`, `cryptography` and `uv`. For DuckDB we
  looked at its settings and one behaviour, as above. For `uv`, which the worker runs to
  build environments, we know only from the code that it is run to install packages from an index. We did
  not check what else it does.
- **Offline extension install.** Lakana cannot point DuckDB at a mirror; see
  [Install without internet access](../how-to/install-without-internet-access.md) for the image,
  or the file to copy, and the setting that stops a download.
- **Other cloud lookups.** We checked the instance metadata address only. The AWS libraries may
  have other places to look for credentials; we did not test them. Lakana sets no `AWS_*`
  variable for the server or the worker, so only what you set reaches them.
- **Postgres and SeaweedFS images.** We did not check the images the Compose bundle starts for
  telemetry of their own. Check their documentation if it matters to you.
- **Other versions.** This page describes the code in this repository at the time it was
  written. A later version may behave differently.
