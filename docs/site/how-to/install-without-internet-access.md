# Install without internet access

After this guide you can run Lakana on machines that cannot reach the internet: you have the
packages or the container images carried in, Python assets build from a package source inside
your network, repositories come from a git server of yours, and the worker never asks DuckDB's
servers for an extension. You need one machine with internet access to fetch files, a way to
carry files to the isolated machines, and the permission to run the services there.

What the guide covers is what Lakana does: it lists the connections it makes and lets you point or
stop each one. It does not make your site compliant with anything. Read
[What leaves the machine](../explanation/what-leaves-the-machine.md) first: it is the list of
connections this page removes.

## Choose how to install

| You run | You carry in | Go to |
|---|---|---|
| Lakana from Docker Compose (the bundle) | The images | [The container images](#the-container-images) |
| Lakana from `pip` (the CLI, local mode, or a server and worker you start yourself) | The wheels, and the DuckDB extension file | [The Python packages](#the-python-packages), [The DuckDB extension](#the-duckdb-extension) |

Either way, read [Python assets](#python-assets-and-their-packages) and
[Git repositories](#git-repositories) next.

## The Python packages

On a machine with internet access, download every wheel `lakana[server]` needs into a folder,
carry the folder to the isolated machine and install from it only, with the package index
switched off:

<!-- not tested: needs the internet, a published release and an isolated machine; checked by hand with 0.21.0, see "How this page was verified" -->
```bash
# on the machine with internet access
pip download "lakana[server]==<version>" -d wheelhouse
pip download uv -d wheelhouse
# for a machine with another Python or platform, add to both, for example:
#   --python-version 3.14 --platform manylinux2014_x86_64 --only-binary=:all:

# on the isolated machine, after carrying the folder over: install from it only
python3 -m venv lakana-env
lakana-env/bin/pip install --no-index --find-links wheelhouse "lakana[server]==<version>" uv
lakana-env/bin/lakana --version
```

Use the same Python version and platform as the target machine, or name them as the comment shows.

`uv` is not a dependency of Lakana. A worker that starts from `pip` builds the environment of a
Python asset with the `uv` it finds on its `PATH` (its start-up log says
`Python assets: enabled (uv at …)`); without it, Python assets are off. A worker that runs from
the container image has `uv`. To check a download, see
[Verify a download](deploy-with-compose.md#verify-a-download): the attestation check needs the
internet and the GitHub CLI, so run it on the machine that downloads.

If `pip` is run on a machine that can reach its package index, it also asks that index whether a
newer `pip` exists. Switch it off with `PIP_DISABLE_PIP_VERSION_CHECK=1`.

## The DuckDB extension

The worker reads table files through DuckDB's `httpfs` extension. A worker that runs from `pip`
downloads it from DuckDB's extension server (`extensions.duckdb.org`) the first time it runs a
query on a machine that does not have it. The container image has it already, so a worker that
runs from the image makes no such request.

Lakana has no setting that points the worker at a mirror of that server. For a `pip` install,
bring the file in yourself and forbid the download:

1. On the machine with internet access, install the extension with **the same DuckDB version and
   platform** as the isolated worker, for the same operating-system user's home:

   <!-- not tested: downloads from DuckDB's extension server; checked by hand with DuckDB 2.0.0, see "How this page was verified" -->
   ```bash
   python3 -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"
   ls ~/.duckdb/extensions/*/*/
   ```

   The file is `~/.duckdb/extensions/v<DuckDB version>/<platform>/httpfs.duckdb_extension`
   (for DuckDB 2.0.0 on Linux: `v2.0.0/linux_amd64`); the folder holds a second small file,
   `httpfs.duckdb_extension.info`. Copy the version folder (`v2.0.0`) to the isolated machine.
2. Put it in `.duckdb/extensions/` in the home directory of the user the **worker** runs as,
   keeping the `v…/<platform>/` folders.
3. Set `LAKANA_WORKER__EXTENSION_SOURCE=none` for the worker (the setting is `duckdb` by default).
   With `none` the worker never downloads, and if the file is missing it stops at start instead of
   trying. The worker's start-up message names the exact path it looked for, for example (a query that finds the file gone later fails with the same text, which begins `Query failed:` and says `Tell the operator:` where this says `To fix it:`):

   > the worker cannot load DuckDB's httpfs extension: /home/lakana/.duckdb/extensions/v2.0.0/linux_amd64/httpfs.duckdb_extension is missing and LAKANA_WORKER__EXTENSION_SOURCE=none forbids downloading it. To fix it: run the worker from the Lakana container image, which bundles it, or copy httpfs.duckdb_extension to /home/lakana/.duckdb/extensions/v2.0.0/linux_amd64/httpfs.duckdb_extension (inside /home/lakana/.duckdb/extensions), or unset LAKANA_WORKER__EXTENSION_SOURCE to let DuckDB download it.

   Use the path in that message, not this one. A new DuckDB release needs its own file: after you
   upgrade Lakana to a release with a new DuckDB, repeat steps 1 and 2.

Other DuckDB extensions are never installed or loaded by the worker. `none` is also a way to make
sure of that on a machine that is allowed to reach the internet: see
[What leaves the machine](../explanation/what-leaves-the-machine.md).

## The container images

A Compose bundle starts these images, and the first three come from registries on the internet:

| Image | Used for |
|---|---|
| `ghcr.io/lakana-labs/lakana:<version>` | The server, the workers, the launcher, `egress`, `migrate` and `setup` |
| `ghcr.io/lakana-labs/lakana-task:<version>-py3.11` to `-py3.14` | The containers that run Python tasks and build their environments (the default runner); only the Pythons your projects name are used |
| `postgres:16` and `chrislusf/seaweedfs:4.47` | The catalog and the object storage |

`docker compose config --images` lists the images of the Compose file; the task images are not in
it, because the launcher starts them from `LAKANA_TASK_IMAGE`. On a machine with internet access
pull each image and save it to one archive, carry the archive, and load it on the Docker host
before `docker compose up`:

<!-- not tested: needs Docker, the internet and a published release; the steps are Docker's own -->
```bash
docker pull ghcr.io/lakana-labs/lakana:<version>
docker pull ghcr.io/lakana-labs/lakana-task:<version>-py3.14
docker pull postgres:16
docker pull chrislusf/seaweedfs:4.47
docker save -o lakana-images.tar \
  ghcr.io/lakana-labs/lakana:<version> ghcr.io/lakana-labs/lakana-task:<version>-py3.14 \
  postgres:16 chrislusf/seaweedfs:4.47

# on the Docker host, after carrying the archive:
docker load -i lakana-images.tar
lakana bundle init --image ghcr.io/lakana-labs/lakana:<version>
```

`lakana bundle init` needs the `lakana` CLI, from the packages above. The images keep their names
when saved and loaded, so the Compose file finds them locally. You can also push them to a
registry inside your network and name it in `--image` and `--task-image`
([Deploying with Compose](deploy-with-compose.md#the-image)).

The image holds the DuckDB extension, `uv` and a current `git`, so nothing else needs to be
fetched. A new Lakana version is a new set of images: repeat the steps.

## Python assets and their packages

A project with Python assets gets its own environment, built by the worker from the project's
`uv.lock`. The build connects to the hosts that lock names, and nowhere else. On an isolated
network that means the lock must name something inside it. A lock may name only the public index,
the folder `wheelhouse` of the project, `LAKANA_RUN__INDEX_URL` and the indexes an administrator
registered ([Sources a lock may name](../reference/package-indexes.md#sources-a-lock-may-name)).
Pick one:

**1. A package mirror inside your network (an index that holds the packages).** Set
`LAKANA_RUN__INDEX_URL` on the server and the workers to its `https://` URL, and in every project
make it the default index so that `uv lock` takes every package from it:

```yaml
# compose.yaml, in the shared block that the server and the workers read:
x-lakana-env: &lakana-env
  # ... the lines that are already here stay ...
  LAKANA_RUN__INDEX_URL: https://packages.internal.example/simple
```

The block is shown, not run. In each project:

```toml
[[tool.uv.index]]
name = "internal"
url = "https://packages.internal.example/simple"
default = true
```

Run `uv lock` on a machine that can reach the mirror, commit `pyproject.toml` and `uv.lock`, and
deploy. The mirror must hold `lakana==<version of your instance>` with its dependencies
(`lakana[assets]` and what it needs) and every package the project uses, as wheels or source
distributions. The lock then names only your mirror. We checked that Lakana's lock check accepts
such a lock with `LAKANA_RUN__INDEX_URL` set and refuses it without.

With the Compose bundle's docker runner, a build reaches only the host of `LAKANA_RUN__INDEX_URL`
instead of the public index, over `https`, on the port of the URL (443 unless the URL says
otherwise); a mirror under `http://` is refused (with the docker runner `LAKANA_RUN__INDEX_URL` refuses every `http://` URL, loopback included, and the worker does not start; only `lakana index add` accepts `http` for `localhost` and loopback addresses). A mirror with a certificate of a private authority
needs `LAKANA_RUN__CA_BUNDLE` on the worker, with the file mounted into it. See
[Package indexes and proxies](run-python-tasks-in-containers.md#package-indexes-and-proxies) for
the proxy (`LAKANA_EGRESS_UPSTREAM_PROXY`), the private authority and the addresses a build never
reaches, and [Network settings](../reference/network-settings.md#the-trust-bundle-of-builds-and-tasks)
for the trust bundle. With the local runner (and local mode) the build runs on the worker's own
network, through `LAKANA_RUN__HTTPS_PROXY` if you set it.

**2. A folder of wheels inside the project.** For a small project, put every wheel of the lock in
a folder `wheelhouse` at the root of the project and make it the default index:

```toml
[[tool.uv.index]]
name = "wheelhouse"
url = "wheelhouse"
format = "flat"
default = true
```

`uv lock` then records each file as a path inside `wheelhouse`, and the build needs no network
at all. Commit the folder with the lock. The folder has to hold `lakana`, `polars` and everything
they need, built for the platform of the worker, so this suits only a few projects. The deploy refuses a path that leaves the folder.

**3. An index registered with a password.** If the internal index asks for one, register it as in
[Use a private package index](private-package-index.md), which also shows the `explicit = true`
and `[tool.uv.sources]` lines it needs; a build that uses a credential installs wheels only.

## Starting a project from a template

`lakana init --template python` runs `uv lock` on the machine where you type it, against the public
package index, and stops with a message when it cannot reach it. On an isolated network, leave out
`--template` (the SQL sample needs no network), or point `uv` at your mirror first: `uv` reads
`UV_DEFAULT_INDEX` (an index URL) and `UV_FIND_LINKS` (a folder of wheels, which the command's own
message also names), and the proxy variables of its environment. Then lock the project as in
"Python assets" above. We did not run this command offline.

## Git repositories

A deploy reads a git commit. The worker runs `git` against the remotes you register and nothing
else; it does not need github.com. Use one of these:

- **A repository on the worker's disk.** `lakana repo add NAME --kind local --url /path` needs no
  network at all. The path must be under one of the instance's `LAKANA_REPO_ROOTS` (the bundle
  mounts `repos/`); see [Projects and deploys](projects.md#register-the-repository-admin).
- **An internal git server over `https` or `ssh`.** Register it with `--kind https` or
  `--kind ssh` as on that page. For a proxy or a private certificate authority, set
  `LAKANA_GIT__HTTPS_PROXY`, `LAKANA_GIT__NO_PROXY` and `LAKANA_GIT__CA_BUNDLE` on the worker
  ([Git network settings](../reference/network-settings.md#git-network-settings)). `ssh`
  repositories ignore all three, and an unknown host key is refused, so give the host's key with
  `--known-hosts-file`.

## Everything else

These need no change; they are listed so that you can check:

- **The database and the object storage** are the bundle's own services, or yours: the connection
  is the one in `LAKANA_DATABASE__URL` and the environment's storage settings. If you use your own
  S3-compatible store with no access keys on a machine that is not a cloud machine, set
  `AWS_EC2_METADATA_DISABLED=true` for the server and the workers, so that the S3 libraries do not
  look for an instance role.
- **CI sign-in (OIDC).** The CI token exchange asks the issuers you list in
  `LAKANA_CI_OIDC__ISSUERS` and is off while the list is empty. Leave it empty on a network that
  cannot reach an issuer.
- **Job notifications** go to the webhook addresses you set; `LAKANA_WORKER__NOTIFICATIONS=false`
  stops a worker from sending any.
- **Docker** pulls an image that is not on the host when you run `docker compose up` or
  `docker compose pull`. The launcher also asks the Docker daemon to pull a task image the first
  time a task needs it and the host does not have it (a project that names `python: "3.12"` needs
  the `-py3.12` image). Load every task image your projects name, as above, or name a registry
  inside your network with `--task-image`; a missing one is pulled from its registry, which an
  isolated host cannot reach, and the task fails.
- **This documentation** is plain files that load nothing from another host. It is built from a
  checkout of the repository with `uv run --group docs zensical build --strict` (output in
  `site-build/`); build it where the internet is and carry the folder in.

## How this page was verified

We checked the parts that we could, on a Linux machine, by cutting the network off for the commands
under test with `unshare -rn` (a new network namespace with no route out; a request to
`pypi.org` there fails with a name-resolution error):

- **Packages.** We ran `pip download "lakana[server]"` with the network, which fetched 73 files, 0.21.0 of Lakana among them, then, in the namespace, `pip install --no-index --find-links wheelhouse
  "lakana[server]==0.21.0"` into a new virtual environment, and `lakana --version`, which printed
  `lakana 0.21.0`.
- **The extension.** With the code of this repository (the setting comes with the release after
  0.21.0), in a fresh home directory we ran the worker's loader with
  `LAKANA_WORKER__EXTENSION_SOURCE=none`: it refused and printed the message above with the path.
  We then copied `v2.0.0` from a home directory where `INSTALL httpfs` had run, and, in the
  namespace, the loader found `httpfs` and loaded it.
- **A package source inside the network.** In the namespace, `uv lock` and
  `uv sync --frozen --no-config` built an environment from a project with a folder `wheelhouse`
  and the `default = true` index above, and Lakana's check of the lock accepted it. We also ran
  the check on a lock that names an `https://` mirror, with and without that mirror as
  `LAKANA_RUN__INDEX_URL`: accepted with it, refused without.

We did **not** test: saving and loading the container images, a Compose stack on a host without
internet access, a real package mirror, or an internal git server. The Docker commands are
Docker's own and are shown as documented there. We run no CI job with the network cut off.
If you find that a step fails on your network, the message of the worker or the build names the
host it could not reach; that host is a connection this page did not list.
