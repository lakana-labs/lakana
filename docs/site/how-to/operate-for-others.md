# Run Lakana for other people

This guide is for the person who installs Lakana and keeps it running for others: an IT
department, a service provider, a computing centre, or the one colleague who "does the servers".
The people who use the instance write SQL and Python; you make sure it is there, safe, backed up
and up to date. You do not need a data engineering background, but you should be able to use a
terminal, Docker and a text editor.

Most of the steps already have a page. This guide puts them in the order an operator needs them
and says what to read at each step; it repeats little. Where nothing is written down yet, it says
so.

## Install

Use the Compose bundle: one machine, one command to generate the files, one to start them. The
steps are in [Deploying with Compose](deploy-with-compose.md#quickstart), and every file and
setting of the bundle is in [The Compose bundle](../reference/compose-bundle.md). Decide four
things before you run it.

**Size the machine by the workers.** A worker is the process that runs queries and tasks, and it
is the part that needs memory.

- Each worker container has a memory limit, `4g` by default (`LAKANA_WORKER_MEMORY` in `.env`),
  and `bundle init` starts two workers by default (`--workers`). Two workers at `4g` need 8 GB
  for the workers alone.
- Add room for PostgreSQL, the object storage and the server. The documentation gives no figure
  for these, so measure on your own data before you promise anything.
- The smallest limit that works is `1g`; a worker needs about 512 MB to start. A worker that
  goes over its limit ends the largest query, so a limit that is too small shows as failed
  queries that say so.
- The limit, what happens at it and how to raise it: [Memory limits](deploy-with-compose.md#memory-limits).
  More workers, and workers for ad-hoc queries only: [Scaling workers](deploy-with-compose.md#scaling-workers).

**Decide where the data lives.** The bundle runs its own object storage (SeaweedFS) in a Docker
volume on the same machine, next to the PostgreSQL volume that holds the catalog. The two volumes
together are the instance: [Backups](deploy-with-compose.md#backups) and the next sections rely
on that. Artifacts (files such as trained models) are kept in the same storage, under a directory
Lakana reserves: [Storage for artifacts](deploy-with-compose.md#storage-for-artifacts). Plan disk
space by the data you expect to store; the documentation names no figure.

**Put TLS in front of it.** By default the server listens on the machine itself, over plain HTTP.
To let others reach it, do not publish the port to the network as it is: put a reverse proxy that
handles TLS in front of it, and pass the public address to `bundle init`. The steps, two example
proxy files and the limits a proxy must allow: [Exposing it](deploy-with-compose.md#exposing-it)
and [A reverse proxy in front of the server](deploy-with-compose.md#a-reverse-proxy-in-front-of-the-server). The proxy passes the browser's `Host` header through (nginx: `proxy_set_header Host $http_host;`;
otherwise the web interface answers 421) and is listed in `LAKANA_TRUSTED_PROXIES`, or the audit
log shows the proxy's address for every client. Set `LAKANA_UI__PUBLIC_URL` to the address people
open, such as `https://acme.lakana.example.org`.

**Keep `.env` safe.** The file holds every secret of the instance, among them the master key
that protects the stored secrets. A lost master key makes every stored secret unrecoverable, so
keep a copy of `.env` with the backups, in a place that only the people who run the instance can
read: [The `.env` file](../reference/compose-bundle.md#the-env-file) and
[Where secrets live](../explanation/security.md#where-secrets-live).

**The first administrator.** `bundle init` creates an administrator named `admin` and saves its
credential in the `team` profile on the machine where you ran it. That credential never expires.
Create a named administrator for yourself and revoke the bootstrap credential, as
[Revoking the bootstrap credential](deploy-with-compose.md#revoking-the-bootstrap-credential)
shows. Only a principal with the `ADMIN` role on the instance can hand out access.

## Hand over

The people you run Lakana for need three things from you: an environment, a grant on it and a way
in that does not involve the server.

**Environments.** An environment is a separate area with its own tables and its own storage. The
bundle creates one, `dev`, in its own object storage. `lakana env create` makes more
([the command](../reference/cli.md#lakana-env-create)), and each environment can be protected
so that only deployed projects change its tables: [Protected environments](projects.md#protected-environments-lockdown-and-break-glass).

A second environment, such as `prod`, needs the bundle's storage and its secret; the commands are in
[Add an environment to a Compose instance](deploy-with-compose.md#add-an-environment-to-a-compose-instance).

**The first grants.** A person who needs to read and query one environment gets the role
`ANALYST` on it, and nothing more. Hide rows or values before you grant access to an environment
that holds data not everyone may see: [Show some people only some rows](filter-rows.md) and
[Mask a column for some people](mask-a-column.md). The commands, in order:

<!-- source: docs/examples/give-access/run.sh#create -->
```bash
lakana principal create alice --type user
```

<!-- source: docs/examples/give-access/run.sh#grant -->
```bash
lakana grant create --principal alice --role ANALYST --kind ENVIRONMENT --env dev
```

<!-- source: docs/examples/give-access/run.sh#token -->
```bash
lakana token issue alice alice-laptop
```

Use your own names. What each command prints, how to see and take away access, and the roles
beyond `ANALYST`: [Give a second person access](give-a-second-person-access.md) and
[Grants, permissions, principals and groups](../reference/access.md).

**People who never touch the server.** A user needs the `lakana` command, the address of the
instance, the environment's name and a token, and nothing else: no account on the machine, no
Docker, no key for the server. They sign in once:

<!-- source: docs/examples/give-access/run.sh#login -->
```bash
printf '%s\n' "$TOKEN" | lakana login --url "$URL" --env dev --token-stdin
```

`$URL` is the public address of the instance, `$TOKEN` the token you gave them. Send the token
over a channel you trust, and never in a file of a git repository. When someone leaves, or loses
a laptop, you take access away in three steps of growing size: one token, one grant or the whole
person ([Take access away](give-a-second-person-access.md#take-access-away)). To answer "why can
she do that?", use [Why may (or may not) someone do this?](why-may-someone-do-this.md).

## Back up and restore

**What to back up.** Two things, together, and a third that is easy to forget:

- the PostgreSQL catalog (it knows every table, grant, secret and run);
- the object storage (it holds the data itself and the artifacts);
- the bundle's `compose.yaml` and `.env`, which keep the master key.

The catalog and the storage are one state. A backup of one without the other taken at another
time leaves them disagreeing.

**How.** The steps and commands are in [Backups](deploy-with-compose.md#backups): stop the server and the
workers, dump the catalog, stop the storage and archive its volume, start everything again. Only
that procedure gives a consistent backup. A backup taken while the stack runs can lose or invent
files whatever the order, so do not rely on one.

**How to restore.** The steps and commands are in [Restore](deploy-with-compose.md#restore): restore
the volume archive and the dump from backups of the same moment, with the `.env` of the same bundle
(a new master key cannot read the old secrets), into the old directory or a new one, then check
a table and the runs. The project tests these steps in CI. Rehearse the restore on a spare
machine before you need it. A restore you have never run is a guess.

**Artifacts after a restore.** Once an hour a worker removes the leftovers of crashed or abandoned
uploads (the sweep). After a restore the catalog and the storage can disagree, and the sweep is
built to remove little then: it keeps a directory the catalog does not know, keeps the files that
a manifest it does not know names, and says so in the worker's log. A file with no record at all
goes after seven days. If you are unsure, set `LAKANA_RUN__SWEEP_INTERVAL: "0"` in the `x-lakana-env`
block of `compose.yaml` before you restore (the bundle's workers do not read it from `.env`) and set it
back after you have checked. What each log line means and what to do
is in [After a restore](deploy-with-compose.md#after-a-restore), and the sweep itself in
[The sweep](deploy-with-compose.md#the-sweep).

!!! note "A gap"
    There is no command to bring an artifact back into the catalog after a restore (nor to make it
    forget one on purpose): an artifact the restored catalog does not know stays unreadable, and
    its files stay in storage. Issue [#68](https://github.com/lakana-labs/lakana/issues/68) tracks
    it. Until then a newer catalog backup is the way to avoid it.

## Upgrade

Every release has the same four steps, in
[What every upgrade asks](../explanation/upgrades.md#what-every-upgrade-asks): back up first,
upgrade the server and every worker together, let the database migrations run before the new
server starts, and read the changelog.

- **Read the breaking changes first.** Each release in the
  [changelog](https://github.com/lakana-labs/lakana/blob/main/CHANGELOG.md) has a section
  "BREAKING CHANGES" when it breaks something, with what changed and how to migrate. Read it for
  every release between yours and the new one. Before 1.0.0 a minor release may break things.
  [Where a breaking change is announced](../explanation/upgrades.md#where-a-breaking-change-is-announced)
  and the table of [the last releases](../explanation/upgrades.md#upgrade-notes-of-the-last-releases)
  tell you which releases need more than a restart.
- **Migrations** run by themselves: the bundle's `migrate` service runs before the server and the
  workers start. The commands to pull and start are in [Upgrading](deploy-with-compose.md#upgrading),
  which also shows how to see whether a newer bundle changed `compose.yaml`.
- **Tell the projects.** After a minor upgrade each project with a Python asset locks `lakana`
  again: [What every minor release asks of projects with Python assets](../explanation/upgrades.md#what-every-minor-release-asks-of-projects-with-python-assets).
- **Rolling back.** No command downgrades an instance. The way back is the backup you took before
  the upgrade, restored with the previous image; it loses everything changed since:
  [Going back](../explanation/upgrades.md#going-back).

## Watch

Look at three things regularly: whether the instance answers, what the logs say, and what people
did.

**Is it healthy?** `docker compose ps` shows whether each service runs and is healthy, and the
address `/readyz` answers `{"status":"ready"}` when the database, the storage and a worker all
work; point your monitoring at it. Two commands show whether workers are registered and which one holds the scheduler role; they need the
right to manage environments on the instance, which an administrator has:

<!-- source: docs/examples/getting-started/run.sh#worker-list -->
```bash
lakana worker list
```

<!-- source: docs/examples/getting-started/run.sh#scheduler-status -->
```bash
lakana scheduler status
```

[Is my instance healthy?](instance-health.md) goes through everything to look at, one question at a
time: the services, the health route, the workers, the queue, the database, the storage and the disk,
with what a bad answer looks like. What the two commands show, and what to do when a query waits:
[Why is my query or task waiting?](why-is-it-waiting.md#what-an-operator-can-look-at).

**Logs worth alerting on.** The server and the workers print to standard error:
`docker compose logs server` and `docker compose logs worker`. Set the level with
`LAKANA_LOG__LEVEL` in the `x-lakana-env` block of `compose.yaml` ([the steps](deploy-with-compose.md#the-sweep)). The lines worth an alert end with a stable code, such as
`event=memory_stop` for a worker that stops for its memory limit
([Memory limits](deploy-with-compose.md#memory-limits)); match alert rules on the code, not on the
wording. Every code and what to do: [Log events](../reference/log-events.md). Two more deserve
an alert and have no code: a sweep that "left an artifact whole" (see
[After a restore](deploy-with-compose.md#after-a-restore)), and a start-up that stops with a
message that names a setting. [The sweep](deploy-with-compose.md#the-sweep)
describes both logs and what the lines look like.

**The audit log.** It records who signed in, what was granted and revoked, who deployed and
ran, and every refusal. Reading it needs the `ADMIN` role; `lakana audit list` filters it
([the command](../reference/cli.md#lakana-audit-list)). What it records: [What the audit log records](../explanation/security.md#what-the-audit-log-records).

**Failing jobs.** Get a message in your chat tool when a scheduled job fails:
[Get told when a job fails](get-told-when-a-job-fails.md).

## One instance per client

If you run Lakana for several clients, give each its own instance. Two bundles fit on one host;
what they must not share is state.

!!! warning "Never two instances on one storage root"
    Lakana keeps files of its own under `artifacts/` in the storage root and treats a file in
    there that its own catalog does not know as a leftover. Two instances on one root remove each
    other's files. The bundle's storage is a volume of its own per bundle, which is why the recipe
    below keeps two bundles apart; if you point an environment at storage you manage yourself,
    give each instance its own root.

What each bundle needs of its own:

| Per client | How |
|---|---|
| A project directory | One directory per client, each from its own `bundle init`. |
| A Compose project name | `COMPOSE_PROJECT_NAME` in the bundle's `.env`, set before the first start. Volumes, networks and containers are named after it. |
| A port | `--port` of `bundle init`; only the server's port is published. |
| Storage and database | Their volumes, named after the project, so separate by the name above. |
| Secrets | `bundle init` generates fresh ones for every bundle. Never copy a `.env`. |
| A CLI profile | `--profile` before `bundle init`, so that the two saved profiles do not collide. |

Why the name matters, and what happens without it: [Running more than one bundle on a host](deploy-with-compose.md#running-more-than-one-bundle-on-a-host).

<!-- not tested: generates and starts two Docker Compose stacks on one host; the example runner has no Docker and two full stacks -->
```bash
lakana --profile acme bundle init clients/acme --port 8001 --public-url https://acme.lakana.example.org
echo "COMPOSE_PROJECT_NAME=lakana-acme" >> clients/acme/.env
(cd clients/acme && docker compose up -d --wait)

lakana --profile berg bundle init clients/berg --port 8002 --public-url https://berg.lakana.example.org
echo "COMPOSE_PROJECT_NAME=lakana-berg" >> clients/berg/.env
(cd clients/berg && docker compose up -d --wait)

lakana --profile acme sql "SELECT 42"
lakana --profile berg sql "SELECT 42"
```

The two `sql` lines work only once the proxy and the DNS name of that client exist, because the profile
uses the public address you gave `--public-url`.

Each `bundle init` saves a profile of the given name, so `--profile acme` or `--profile berg` before
a command picks the instance. Check that the two stacks are separate:

<!-- not tested: lists the Docker Compose projects of a host that runs two stacks; the example runner has no Docker -->
```bash
docker compose ls
```

Upgrade one client and leave the other alone: change `LAKANA_IMAGE` in that client's `.env`, then
pull and start from that client's directory. Do the same backup first, as in
[Upgrading](deploy-with-compose.md#upgrading).

<!-- not tested: pulls a new image and restarts one of two Docker Compose stacks -->
```bash
cd clients/acme
docker compose pull && docker compose up -d --wait
```

Plan memory for both: the workers of every bundle add up (two bundles of two workers at `4g` need
16 GB for the workers). Back up each bundle on its own, with the volume names of its own project
(`lakana-acme_pgdata` and `lakana-acme_seaweed` for the one above). The recipe has not been run
in the project's automated tests, because they need Docker and two complete stacks.

## What the project does and does not provide

Lakana is run by one maintainer. There is **no support contract and no service level**: no
response times, no warranty, and no promise that a question gets an answer or a bug a fix. Fixes
go into the latest release only. Whoever operates an instance, for a client or for colleagues,
does so on their own account, and any promise you make to the people you run it for is yours, not
the project's. [Support](../explanation/support.md) says where to ask and how to report a problem, and
[Security](../explanation/security.md) says what the software protects and what it does not.
