# Run Python tasks in containers

With `LAKANA_RUN__RUNNER=docker`, every Python task runs in a container of its own, and every
environment build in another one. The Compose bundle does this by default. This page says what
that isolates and what it does not, how to set it, and what each message means.

Two services of the bundle do the work. `launcher` is the only one that holds the Docker socket:
a worker asks it, over a Unix socket, to start a task or a build. `egress` is the only way out of
a build container, to the package indexes the build may use, and of a task container, to the
connections its asset declares.

## What it isolates

- **The worker's secrets and files.** A task sees its project's code (read-only, at `/code`), its
  built environment (read-only, at `/env`), a private scratch directory (`/work`) and a private
  `/tmp`. Nothing of the worker's environment variables, settings, files or processes, and not
  the Docker socket. Its task token arrives on its standard input, as with the local runner.
- **Other projects.** Each task has a network of its own, which no other task shares. A task
  whose asset declares no connection finds only the Lakana server there: not the host, not the
  internet, not the database or the object store. A task with connections reaches exactly their
  endpoints and nothing else, through the `egress` proxy and a relay of its own (see
  [Connections: what a task reaches](#connections-what-a-task-reaches)). A task resolves no name
  outside its network. An environment belongs to one project in one
  environment and is built for its `uv.lock`, `pyproject.toml`, Python and set of package indexes,
  by another user than the one the task runs as, so a task cannot change it.
- **Resources.** Memory, CPUs and processes are limits of the container (`2GB`, `1` and `256` by
  default), enforced by the kernel. A task over its memory ends with "this task was ended because
  it used more than its container's memory limit" and is retried while it has attempts left.
- **Leftovers.** When a task ends, is cancelled or times out, or its worker stops or dies (its
  connection to the launcher closes), the container is removed with every process in it,
  including those the task started in the background. An environment build is stopped the same
  way: a cancel, a drain past its grace or a lost lease kills the build container, and nothing of
  the half-built environment is kept, so the next run builds it again.

Inside the container a task runs without capabilities, with a read-only root filesystem, as uid
10001 in a group of its own, and with `no-new-privileges` where Docker allows it.

## What it does not

- **Containers share the host's kernel.** This isolates projects from each other and from the
  worker. It is not a sandbox for code written to break out of a container: keep reviewing what
  you deploy.
- **A task still reaches the server.** It talks to Lakana's REST API with its own task token, as
  before, and it can connect to every port the `server` container listens on.
- **A declared host gets what the task sends it.** A task reads the credentials of the
  connections its asset declares and can send them, and any data it reads, to any endpoint of
  those connections. Review an asset's `connections` with its code. Its egress token is in its
  environment (`LAKANA_EGRESS_PROXY`), but it is bound to the task's own network and lifetime: a
  copy in another container, or after the task, opens nothing.
- **The Docker host's own addresses, unless you list them.** `egress` and the relays refuse the
  Docker host's gateway addresses and names, but not its LAN, VPN or other addresses: list them in
  `LAKANA_EGRESS_REFUSED` (see [Package indexes and proxies](#package-indexes-and-proxies)).
- **A `tcp` connection is encrypted only if your driver encrypts it.** The relay forwards bytes
  and never looks at them; TLS, where the protocol has it, stays between the task and the source.
- **Builds run your dependencies' build code.** A package built from source (a source
  distribution) runs its own code in the build container. That container holds the credentials of
  the package indexes its lock uses and the password in `LAKANA_RUN__INDEX_URL`, if it has one, and
  can reach those indexes' hosts. When the lock uses a registered package index with a credential,
  Lakana installs wheels only and runs no package's build code. Prefer wheels, and review
  dependencies as you review code.
- **The launcher holds the Docker socket, which is root on the host.** It has no network, no
  capabilities and a read-only filesystem, and it takes only a fixed set of requests from the
  workers; a fault in it is still a fault on the host. Protect the host as you would any Docker
  host, and give the Docker group only to people you would give root.
- **Workers trust each other.** A compromised worker can stop another worker's task or build only
  if it learns that task's id: 128 random bits that each worker makes for every task and build
  and neither stores nor logs. The name a worker gives the launcher (`launcher-owner` in the
  `worker-cache` volume, which every worker of the bundle shares) is readable by the other
  workers, so it protects nothing by itself.
- **Disk.** A task's scratch directory, the built environments and the build caches have no disk
  quota (Docker volumes have none on most storage drivers). An environment larger than
  `LAKANA_RUN__VENV_MAX_BYTES` is refused once it is built; nothing stops a build from filling the
  disk while it runs. The launcher keeps the 10 most recently used environments and removes a
  build cache that nobody used for 14 days.
- **The local runner and local mode.** `LAKANA_RUN__RUNNER=local` and local mode
  (`lakana server start --local`) run tasks as the worker's own processes, so one instance stays
  one trust domain: see [Isolation](../explanation/python-assets.md#isolation-one-instance-is-one-trust-domain).

## Turn it on or off

In the Compose bundle it is on: `LAKANA_RUNNER=docker` in `.env`. To go back to tasks as the
worker's own processes, set `LAKANA_RUNNER=local` and run the `docker compose` command of the
[Quickstart](deploy-with-compose.md#quickstart) again. `launcher` and `egress` still start either
way (see [Python assets](deploy-with-compose.md#python-assets) in "Deploying with Compose"). Local
mode always runs tasks as processes.

Docker Engine 28 or later is required: it is the first that keeps a task's network away from the
host. On Docker installed as a snap, `no-new-privileges` is left off (the launcher logs
"no-new-privileges is off: this Docker refuses it for task and build containers."); the other
protections stay.

Budget the host's memory for the workers' own limits plus one task's memory
(`LAKANA_RUN__TASK_MEMORY`) per worker: a task container's memory is outside the worker's limit.

## Connections: what a task reaches

A task reaches exactly the endpoints of the [connections](../reference/connections.md) its asset
declares (`@asset(connections=["crm-db"])`), and nothing else:

- **HTTPS** (`http`, `s3`, `gcs` and `azure` connections) goes through `egress`, with a token that
  names the connection's host and port only, in the task's `LAKANA_EGRESS_PROXY`. Lakana's helpers
  and `connections.get(...).proxy_url` use it; a library you call yourself needs it passed as its
  proxy, because the task has no `HTTPS_PROXY`.
- **Databases and `tcp`** (`postgresql`, `mariadb` and `tcp` connections) go through a relay of
  the task's own: a small container on the task's network, `lakana-relay`, with one port per
  connection from 15001, each forwarded to that connection's host and port only.
  `connections.get(...).relay_address` names it. The relay never reads the bytes, so TLS stays
  end to end: Lakana's `sql.read` checks the real host name as before. A client of your own checks
  it only if it takes the host name apart from the address it dials; for a `tcp` connection that
  is up to its driver ([the SQL Server recipe](read-from-a-sql-server-database.md#tls) shows one
  that does).
- **Directories** (`directory` and `sqlite-file` connections) are read-only mounts from
  `LAKANA_LAUNCHER__INGESTION_ROOTS` (see [Ingestion roots](#ingestion-roots)); the task reads its
  connection at `/ingest/<root>/<path>`. The launcher mounts the **whole root**, so a task can
  also read the other paths under that root, those of other connections included. Give each
  group of data that some tasks must not read a root of its own.

What a task cannot reach, with or without connections: another task, the Lakana stack (the
database, the object store, the launcher; the server only through its own network), the cloud
metadata address, the Docker host's loopback, gateways and names, and the ranges of
`LAKANA_EGRESS_REFUSED`. A connection's host that resolves to one of these is refused whatever
the asset declares. The full list is in
[Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach).

**The `egress` service is required** for a task with an HTTPS or database connection. Without it
running, such a task fails without retry: ``tasks with connections need the `egress` service;
start it (Compose bundle) or remove the connection from the asset.`` A task without connections, or
with directory connections only, does not need it.

**Limits.** A task reaches at most 32 HTTPS hosts (16 connections of at most two endpoints each,
whose host names must also fit in one egress token) and 16 database endpoints; an asset over a
limit fails without retry, naming it. A relay has 64 MiB
and carries at most 16 connections at once per database connection; the 17th is closed, and the
task's log says so. A connection the relay refuses or closes is explained in the task's log, on a
line that starts with `lakana-relay:`.

**Certificates.** A source behind a private certificate authority: give the connection its
`ca_certificate` (PostgreSQL, MariaDB, `http`), or set `LAKANA_RUN__CA_BUNDLE` for every build and
task (see [Package indexes and proxies](#package-indexes-and-proxies)).

**A company proxy.** `LAKANA_EGRESS_UPSTREAM_PROXY` applies to tasks and their relays too. Before
`egress` asks that proxy, it refuses a host that resolves, on the Docker host's network, to one of
the addresses above.

**A source on the Docker host.** Its gateway address (such as `172.17.0.1`) and
`host.docker.internal` are always refused. Reach it by another address of the host that you do
not list in `LAKANA_EGRESS_REFUSED`, or better, move it off the host: every address of the
Docker host that you leave out of that list is reachable by every task whose connection names it.

## Choose the Python

Tasks run on the worker's Python (3.14 in the bundle's image) unless the project's `lakana.yaml`
names another:

```yaml
python: "3.12"
```

The versions are 3.11 to 3.14, in quotes. A worker without containers refuses a project that names
another Python than its own. See [The Python of your tasks](../reference/lakana-yaml.md#the-python-of-your-tasks).

## Package indexes and proxies

A build reaches only the hosts of the package indexes its project may use: the public index
(`pypi.org` and `files.pythonhosted.org`), or instead the host of `LAKANA_RUN__INDEX_URL`, and the
registered package indexes its lock names. A package's files must come from one of those hosts.
`egress` refuses anything else, and the build fails with uv's error in the task's log.

- **Ports.** A build reaches each of those hosts on the port of the index's URL: port 443 unless
  the URL names another (`https://pkgs.example.org:8443/simple`), with a credential or without.
  Another port of the same host stays closed.
- **https only.** `egress` tunnels https, so an `http://` index cannot be reached from a build,
  and an `http://` `LAKANA_RUN__INDEX_URL` stops a worker with the docker runner at start.
- **Addresses.** `egress` never connects to loopback, link-local (cloud metadata), multicast or
  reserved addresses, into the networks it is attached to (the bundle's own network with the
  server, the database and the object store, and the build networks), to the gateway of any
  Docker network (such as `172.17.0.1`, where the Docker host answers), to the Docker host's
  names (`host.docker.internal` and the like), nor to the ranges of `LAKANA_EGRESS_REFUSED` in
  `.env`. Other private addresses stay reachable, because a company's package index often has
  one. List every address of the Docker host in `LAKANA_EGRESS_REFUSED` (`ip -br addr`): its LAN
  address or subnet, a VPN or Tailscale address (`100.64.0.0/10`), `virbr0`, and on Docker
  Desktop `192.168.65.0/24`, plus any other internal network a task must never reach; bind
  services on the Docker host to loopback (`127.0.0.1`), not `0.0.0.0`. Docker's gateways and
  the host names are refused without it.
  Details: [Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach).
- **A company proxy.** If your network sends https through a proxy, set
  `LAKANA_EGRESS_UPSTREAM_PROXY` in `.env` (`http://[user:password@]host:port`). `egress` then
  tunnels through that proxy. It still refuses a numeric host in the ranges above before it asks
  that proxy, which resolves names itself, so keep internal ranges refused in its rules too; it
  still lets a build reach only its own indexes' hosts.
- **A private certificate authority.** If an index (or a source your tasks read) uses a
  certificate a private authority signed, mount that authority's PEM file into the `worker`
  service read-only and set `LAKANA_RUN__CA_BUNDLE` on the worker to its path (see
  [Deploy with Compose](deploy-with-compose.md#python-assets)). Builds and tasks then trust
  Mozilla's roots plus that file: the worker mounts it read-only at `/trust/trust.pem` in each
  build and task container and sets `SSL_CERT_FILE` and `LAKANA_CA_BUNDLE` to it. Only the file's
  certificates are used; one with a private key stops the worker. Details and every start-up
  message: [The trust bundle of builds and tasks](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks).
- **The local runner's proxy.** `LAKANA_RUN__HTTPS_PROXY` applies only to the local runner and
  stops a worker with the docker runner at start; use `LAKANA_EGRESS_UPSTREAM_PROXY` instead.

A secret is sent only to the index it is bound to: see [Use a private package index](private-package-index.md).

## Protected environments

A protected environment runs Python tasks only on workers with `LAKANA_RUN__RUNNER=docker`. A
worker with the local runner fails such a task at once (failure code `no_runtime`), so local mode
cannot run the Python assets of a protected environment; SQL assets are not affected. Whether an
environment is protected is read when a task's attempt is prepared: an attempt that was already
under way on a local-runner worker when you protected the environment runs to its end there.
Give every worker that takes pipeline work the docker runner before you protect an environment
with Python assets (see [Protected environments](projects.md#protected-environments-lockdown-and-break-glass)).
This is also the security condition, not only a matter of where tasks run: a task on the local
runner, of any environment, runs as the worker's own user and can read the worker's environment,
which holds the database URL (`LAKANA_DATABASE__URL`) and the master key
(`LAKANA_SECRETS__MASTER_KEY`). With those it could write to a protected environment and read every
secret directly, and protection would mean nothing.

## Settings

### The worker

Set these where the worker reads them (in the bundle, under `x-lakana-env` in `compose.yaml`).
Every variable is also in the [settings reference](../reference/settings.md).

| Setting | Default | What it does |
|---|---|---|
| `LAKANA_RUN__RUNNER` | `local` (`docker` in the bundle) | How tasks run. |
| `LAKANA_RUN__LAUNCHER_SOCKET` | `/run/lakana-launcher/launcher.sock` | The launcher's socket, in the volume the worker shares with it. |
| `LAKANA_RUN__TASK_MEMORY` | `2GB` | A task's and a build's memory, outside the worker's own limit. |
| `LAKANA_RUN__TASK_CPUS` | `1` | A task's and a build's CPUs. |
| `LAKANA_RUN__TASK_PIDS` | `256` | A task's and a build's most processes. |
| `LAKANA_RUN__INGESTION_ROOTS` | none | Local runner only: host directories, by name, that a `directory` or `sqlite-file` connection reads. The docker runner ignores it; see [Ingestion roots](#ingestion-roots). |

`LAKANA_RUN__VENV_BUILD_TIMEOUT`, `LAKANA_RUN__VENV_MAX_BYTES`, `LAKANA_RUN__KILL_GRACE` and
`LAKANA_RUN__INDEX_URL` apply to both runners. `LAKANA_RUN__VENV_CACHE`, `LAKANA_RUN__VENV_KEEP` and
`LAKANA_RUN__NO_PROXY` apply to the local runner only, and so does the trim of the worker's package
cache (`LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES`): with the docker runner the environments and
caches are the launcher's volumes.

### The launcher

Set these under `environment:` of the `launcher` service in `compose.yaml`; the bundle sets the
first two. Keep your additions when you replace `compose.yaml` on an upgrade.

```yaml
  launcher:
    # ... the lines that are already here stay ...
    environment:
      LAKANA_LAUNCHER__NETWORK_RANGE: 10.99.0.0/16
```

| Setting | Default | What it does |
|---|---|---|
| `LAKANA_LAUNCHER__STACK` | the Compose project name | Prefixes every container, network and volume the launcher makes, and labels them `lakana.launcher.stack=<project>`. |
| `LAKANA_LAUNCHER__IMAGES` | from `LAKANA_TASK_IMAGE` | The allowed task images, as JSON from Python version to image. |
| `LAKANA_LAUNCHER__SOCKET` | `/run/lakana-launcher/launcher.sock` | Where the launcher listens; the token the workers send lies next to it. |
| `LAKANA_LAUNCHER__NO_NEW_PRIVILEGES` | `auto` | `auto` sets `no-new-privileges` where Docker allows it; `on` refuses to start where it does not. |
| `LAKANA_LAUNCHER__MAX_MEMORY_MB` | `16384` | The most memory one task or build may ask for, in MiB. |
| `LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB` | `65536` | The most memory the running tasks and builds may hold together; one more waits as "runner unavailable" and is retried. |
| `LAKANA_LAUNCHER__MAX_CPUS` | `8` | The most CPUs one task or build may ask for. |
| `LAKANA_LAUNCHER__MAX_PIDS` | `4096` | The most processes one task or build may ask for. |
| `LAKANA_LAUNCHER__NETWORK_RANGE` | `10.207.0.0/16` | The private IPv4 range (a `/24` or larger) that each task and build network gets 8 addresses from. |
| `LAKANA_LAUNCHER__EGRESS_REFUSED` | none (from `LAKANA_EGRESS_REFUSED` in `.env`) | Address ranges builds, tasks and connection tests never reach, besides Docker's gateways and the ranges always refused: comma-separated CIDR ranges or addresses, at most 64, such as `192.168.1.0/24,10.0.0.7`. List the Docker host's LAN address or subnet. A malformed entry stops the launcher, naming it. |
| `LAKANA_LAUNCHER__INGESTION_ROOTS` | none | Host directories, by name, that a task may read at `/ingest/<name>`, as JSON such as `{"shared": "/srv/landing"}`. A task gets only the roots its `directory` and `sqlite-file` connections name. |
| `LAKANA_LAUNCHER__PROBE_MAX_ENTRIES` | `100000` | The most entries the start-up check looks at in one ingestion root. |

**The network range.** Task and build networks take their addresses from
`LAKANA_LAUNCHER__NETWORK_RANGE`. If your network uses `10.207.0.0/16` (a VPN, a site, a company
package index), the host would send that traffic into a task network instead: choose a range your
network does not use. A `/16` holds 6,144 networks at once; the addresses of a build's network
are not given out again until a minute after its egress token expires.

#### Ingestion roots

A `directory` or `sqlite-file` connection names a root (`--address
'{"root": "shared", "path": "exports"}'`), and each worker maps that name to a host directory:
the launcher with `LAKANA_LAUNCHER__INGESTION_ROOTS` for the docker runner, the worker itself with
`LAKANA_RUN__INGESTION_ROOTS` for the local runner. Both take the same JSON object, such as
`{"shared": "/srv/landing"}`, and refuse the same directories; a refused value stops the
launcher or the worker at start, naming the setting. The Compose bundle sets the launcher's from
`LAKANA_INGESTION_ROOTS` in `.env` ([Files on this host](deploy-with-compose.md#files-on-this-host)). A task whose connection names a root its
worker lacks fails without retry: ``` `exports` names ingestion root `shared`, which this worker
does not have ... ```.

The launcher refuses `/`, a directory under or holding `/run`, `/var/run`,
`/var/lib/docker`, `/var/lib/containerd`, `/var/snap/docker`, `/proc`, `/sys`, `/dev`, `/etc`,
`/root` or `/boot`, and the Docker socket's or its own socket's directory. At start it looks at each root
from inside a container and refuses
requests for a root that holds a socket, a pipe, a device or a system mount. Docker follows a link
on the host when it mounts a root, which the launcher cannot see, so three rules are yours to keep:
a root is not a symbolic link; neither the root nor any of its parent directories is writable by
anyone but root; and the Docker group's id on the host is none of 10001, 10002 and 10003, the ids
Lakana's containers run as (the launcher refuses to start when it is 10002 or 10003).

### The egress proxy

Set these under `environment:` of the `egress` service in `compose.yaml`.

| Setting | Default | What it does |
|---|---|---|
| `LAKANA_EGRESS__UPSTREAM_PROXY` | none (from `LAKANA_EGRESS_UPSTREAM_PROXY` in `.env`) | The proxy builds, tasks and their relays go through, `http://[user:password@]host:port`. |
| `LAKANA_EGRESS__MAX_CONNECTIONS` | `256` | Connections at once, 1 to 4096. |
| `LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK` | `32` | Connections one build or task may hold at once, 1 to `LAKANA_EGRESS__MAX_CONNECTIONS`, so that one build cannot take the connections the others need. A build downloads at most 16 packages at once; keep this at 24 or more. A build past it gets `503` with the reason in uv's error. |
| `LAKANA_EGRESS__LISTEN` | `0.0.0.0:3128` | Where the proxy listens. Leave it. |
| `LAKANA_EGRESS__KEY` | `/run/lakana-egress/egress.key` | The key the launcher signs build and task tokens with; a new one at every launcher start. Leave it. |

## Remove everything, or recover from an incident

Everything the launcher makes carries the label `lakana.launcher.stack=<project>`, where
`<project>` is the Compose project name (`lakana` unless you set `COMPOSE_PROJECT_NAME`).
`docker compose down -v` does not remove it; the commands are under
[Starting over](deploy-with-compose.md#starting-over).

If a project may have deployed a malicious dependency or package, or a worker may be compromised:

1. Stop the workers, so that nothing builds or runs meanwhile.
2. Run the three commands of [Starting over](deploy-with-compose.md#starting-over) that remove the
   launcher's containers, networks and volumes, but not `docker compose down -v`, which deletes
   your data. Every environment is rebuilt from its lock on next use.
3. Rotate what a build or a task could have seen: the credentials of the registered package
   indexes the affected projects use, and the password in `LAKANA_RUN__INDEX_URL` if it has one.
4. Deploy the fixed project, start the workers again, and read the audit log and the task logs of
   the affected projects for what ran meanwhile.

## When something goes wrong

| Message | Cause | What to do |
|---|---|---|
| `runner unavailable: ... Check that the launcher service is running ...` | The worker cannot reach the launcher, Docker failed, or the launcher's memory or network limit is reached. | Look at the state of `launcher` in `docker compose ps` and at `docker compose logs launcher`. The task is retried. |
| `Docker Engine 28 or later is needed` (launcher log) | The engine cannot keep a task's network away from the host. | Upgrade Docker. |
| `LAKANA_LAUNCHER__NO_NEW_PRIVILEGES is on, but this Docker cannot ...` (launcher log) | Snap Docker refuses the option. | Set it to `auto`. |
| `The group of the Docker socket (...) is a group that task or build containers run in` (launcher log) | The Docker group's id is 10002 or 10003. | Give the Docker group another id on the host. |
| `The Docker socket ... is writable by everyone` (launcher log) | The socket's mode lets any local user control Docker. | Restrict it to the docker group on the host: `chmod 660`, owned by root:docker. |
| `this task was ended because it used more than its container's memory limit` | The task used more than `LAKANA_RUN__TASK_MEMORY`. A task process killed by signal 9 that Lakana neither stopped nor timed out reads the same way, because the memory limit is by far the most likely sender; a task that kills itself, an operator's `docker kill` or `docker rm -f`, or the host's own OOM killer also end a task with that signal and read as this message. | Read less in the task, or raise the setting. |
| `... is not a destination this build may reach` (in uv's error), `Refused a build connection ... not in its token.` (egress log) | The build tried a host or port that is not one of its indexes. | Register the index (`lakana index add`), or set `LAKANA_RUN__INDEX_URL`, with the port in its URL; see [Package indexes and proxies](#package-indexes-and-proxies). |
| `... is not a destination this connection test may reach; check the connection's address with lakana connection show` (`lakana connection test`, outcome `not_allowed`) | The test's token names exactly the connection's endpoints, and the check asked for something else; with a matching Lakana release on the launcher and the worker this does not happen. | Check the address (`lakana connection show NAME`); if it is right, upgrade the launcher and the workers to the same release. |
| `the relay refused HOST:PORT: cannot reach ...` or `... cannot resolve ...` (`lakana connection test`) | A database or `tcp` connection's test went through the relay, which could not reach the source. | Check the host and port, and that the source accepts connections from the Docker host. |
| `the task image has no connection check (/opt/lakana-check/check.py and its environment /opt/lakana-check-env) ...` (`lakana connection test`, outcome `failed`) | The launcher's task image is older than the worker (an image of an earlier release has no check environment), or was built from an older `Dockerfile.task`. | Pull or rebuild the task images of this Lakana version (the images in `LAKANA_LAUNCHER__IMAGES`), then test again. |
| `the relay did not reach HOST:PORT within 20 s ...` (`lakana connection test`, outcome `timeout`) | The relay reported nothing within 20 s: the host name resolved slowly, it has several addresses that each dropped the connection, or the relay itself died. (A single firewall that drops packets shows as the relay's own `... cannot reach ... (timed out)` after 15 s, the row above.) | Allow connections from the Docker host to the source's port, check that the host name resolves quickly, then test again. |
| `the relay refused HOST:PORT: the upstream proxy refused ...` or `... cannot reach the upstream proxy ...` (outcome `failed`) | The company proxy set in `LAKANA_EGRESS__UPSTREAM_PROXY` refused or did not answer. | Read `docker compose logs egress` and check the upstream proxy. |
| `... is not a destination this task may reach; declare it in @asset(connections=[...]) ...` (the task's log) | The task tried a host or port that none of its asset's connections names. | Create a connection for it (`lakana connection create`), declare it in the asset's `connections`, and deploy; see [Connections: what a task reaches](#connections-what-a-task-reaches). |
| ``tasks with connections need the `egress` service; start it (Compose bundle) or remove the connection from the asset.`` | The asset declares an HTTPS or database connection and the `egress` service is not running. | Start it: `egress` is part of the bundle's `compose.yaml`; look at `docker compose ps` and `docker compose logs egress`. |
| `runner unavailable: ... the task's relay did not start: ...` | The relay of a task with a database connection did not report ready within 10 seconds; the message ends with its last line. | Look at `docker compose logs launcher`. The launcher and `egress` run the same release's image: upgrade them together. The task is retried. |
| `runner unavailable: ... a task's relay goes out through the egress service's own network ...` | The `egress` container is on more or fewer than one network that the launcher did not create. | Attach `egress` to exactly one network, the bundle's `default`, as `compose.yaml` does. |
| `lakana-relay: closed a connection to ...: 16 are open already, the most one connection of a task may hold` (the task's log) | The task opened more than 16 connections at once to one database connection. | Use fewer at once: a connection pool of 16 or fewer. |
| ``LAKANA_LAUNCHER__EGRESS_REFUSED: `...` is not a network (host bits set; did you mean ...?)`` or ``... is not an IP address or CIDR range`` (launcher log; it stops) | An entry of `LAKANA_EGRESS_REFUSED` in `.env` is not a CIDR range or address, or a range has host bits set. | Write the range the message suggests, or a plain address, separated by commas; see [Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach). |
| `cannot list Docker's networks to find their gateways ...` (launcher log; it stops) | Docker did not answer the network listing, so the launcher cannot tell which addresses are the Docker host's. | Check the Docker daemon (`docker network ls`) and `docker compose logs launcher`, then start the launcher again. |
| `... is not reachable through this proxy: it resolves to a loopback, link-local, metadata or internal address ...` (in uv's error or the task's log), or `lakana-relay: refused <host>:<port>: ...` (the task's log) | The host is loopback, metadata, part of the stack, a Docker network's gateway, a name of the Docker host such as `host.docker.internal`, or in `LAKANA_EGRESS_REFUSED`. A task's message ends with `give the connection an address outside the Lakana stack (lakana connection update)`. | Give the index or the connection an address outside those ranges (`lakana connection update`), or take its range out of `LAKANA_EGRESS_REFUSED`; see [Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach). |
| `the egress proxy cannot read the refused ranges the launcher writes ...` (`503`, in uv's error or the task's log) | The launcher has not written `refused.json` to the `egress-key` volume yet, or it is damaged. | Look at `docker compose logs launcher`; the file is written at every launcher start, so `docker compose restart launcher` writes it again. |
| `a task's relay refuses at most 256 address ranges ...` | The host has so many Docker networks that their gateways and your ranges do not fit in a relay's list. | Remove the Docker networks nobody uses (`docker network prune`), or list wider ranges in `LAKANA_EGRESS_REFUSED`. |
| `this client's network already holds 32 connections to the egress proxy ...` (in uv's error or the task's log) | The build or task has `LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK` connections open already: its own code, a build backend for example, opens many. | The task is retried. If it keeps happening, raise the setting under `egress` in `compose.yaml`; see [The egress proxy](#the-egress-proxy). |
| `python: must be one of "3.11", "3.12", "3.13", "3.14" ...` | The version has no task image, or lacks quotes. | Write one of the four, in quotes. |
| `this project names Python X in lakana.yaml, and this worker runs Python tasks on its own Python Y without containers ...` | A local-runner worker took the task. | Run it on a worker with `LAKANA_RUN__RUNNER=docker`, or remove `python:`. |
| `the launcher refused the task: run takes exactly the fields ...` or `the launcher refused the build: build takes exactly the fields ...`, with `The worker and the launcher are different releases` | The worker and the launcher run different releases (one was upgraded without the other), so they disagree on what a request holds. | Upgrade both to the same release: `docker compose pull`, then `docker compose up -d`. Every task fails until they match. |
| `the launcher refused the task: ...` | Worker and launcher settings disagree. | Compare `LAKANA_LAUNCHER__IMAGES` and the `LAKANA_LAUNCHER__MAX_*` ceilings with the `LAKANA_RUN__TASK_*` settings. |
| `environment 'prod' is protected, and this worker runs Python tasks without isolation ...` | A local-runner worker took a protected environment's task. | Give the worker `LAKANA_RUN__RUNNER=docker`. |
| `LAKANA_RUN__HTTPS_PROXY applies to builds of the local runner ...` (worker log) | A local-runner setting on a docker-runner worker. | Unset it, and set `LAKANA_EGRESS_UPSTREAM_PROXY` in `.env` for a proxy; see [Package indexes and proxies](#package-indexes-and-proxies). |
| `LAKANA_RUN__CA_BUNDLE names /x, which ...` (the worker stops at start) | The file is missing or unreadable in the worker, over 1 MiB, holds no certificate, a damaged certificate, a private key, or a `TRUSTED CERTIFICATE` block. | Do what the message's cause says; each cause and its fix: [The trust bundle of builds and tasks](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks). |
| `a package index's certificate is not trusted; if your package index uses a private certificate authority, set LAKANA_RUN__CA_BUNDLE ...` (in a build's failure) | uv could not verify the index's certificate (`invalid peer certificate: UnknownIssuer`). | Set `LAKANA_RUN__CA_BUNDLE` to the PEM file of the authority that signed it; see [Package indexes and proxies](#package-indexes-and-proxies). |
| ``` `exports` names ingestion root `shared`, which this worker does not have ... ``` | The connection's root is not in `LAKANA_LAUNCHER__INGESTION_ROOTS` (docker runner) or `LAKANA_RUN__INGESTION_ROOTS` (local runner) of the worker that took the task. | Add it to that setting and restart the launcher or the worker; see [Ingestion roots](#ingestion-roots). |
| `LAKANA_RUN__INGESTION_ROOTS must be a JSON object from a name ...` (the worker stops at start) | A root is not an absolute directory, or is a system directory, the Docker socket's or the launcher socket's directory. | Give each root a directory of its own outside those, such as `/srv/landing`. |
| `timed out: the launcher reported no end of the task's container ...` | The launcher did not report the container's end within the task's timeout, its kill grace and 15 minutes. | Read `docker compose logs launcher`; the worker closed its connection, which removes the container. |
