# Use a private package index

Your team keeps shared code in a package of its own that is not on the public package index
(PyPI). This page shows how a project's Python assets install it from a package index that asks
for a password. An administrator registers the index once. The password is a secret of each
environment. It never goes into the project's files, and Lakana does not hand it to a task's own
process.

Before you register an index: with the local runner (`LAKANA_RUN__RUNNER=local`, and local mode) a
task runs as the worker's operating-system user, so everyone who may deploy Python code to the
instance can read the index's credential. With the docker runner, the Compose bundle's default, a
task's container holds no credential, and a build that uses one installs wheels only (see
[Run Python tasks in containers](run-python-tasks-in-containers.md),
[Isolation: one instance is one trust domain](../explanation/python-assets.md#isolation-one-instance-is-one-trust-domain)
and the [threat model](https://github.com/lakana-labs/lakana/blob/main/docs/security/threat-model.md)). Register an index only with a credential
you would give those people.

You need: an instance that runs Python assets ([Projects and deploys](projects.md), "Python
assets"), a package index that serves your package over HTTPS as a **wheel**, and a user name and
a password or token for it.

## 1. Register the index (administrator, once)

<!-- source: docs/examples/getting-started/run.sh#private-index-register -->
```bash
lakana secret create team-index --env dev --values-stdin < credential.json
lakana index add team --url https://pkgs.example.org/simple --credential-secret team-index
```

`credential.json` holds the user name and the password:

```json
{"username": "ci-reader", "password": "the-password-or-token"}
```

For an index that takes a token, put the token into `password` and use the user name the index
asks for with tokens. Delete the file afterwards.

- `team-index` is the name of a secret, the one the index names. Create it before you register
  the index, in **every environment** whose projects use the index. Environments can hold
  different passwords under the same name. Registering the index binds each secret of that name
  to it. That takes the right to manage secrets (the `ADMIN` role) in every environment that has
  an unbound secret of that name; if you lack it in one, the command is refused and nothing is
  bound. A secret created after the index is not bound yet: create it with its use, or bind it,
  as [Manage secrets](manage-secrets.md#bind-a-secret-to-its-use) shows. A deploy into an
  environment that lacks the secret stops and says so; a build with a secret that is not bound to
  the index is refused, and the message gives the command that binds it.
- A secret has one use: this index, a repository, or its environment's own work (storage and
  webhooks). Lakana sends it nowhere else. `lakana secret list --env ENV` shows each secret's use.
- An index that asks for no password can be registered too: leave out `--credential-secret`.
- Registering needs the permission `MANAGE_PACKAGE_INDEX` on the instance. The `ADMIN` role has
  it, and an administrator can also grant it to someone as a single action. Whoever holds only
  that permission can register an index at any URL, but can name only a secret that an
  administrator has already bound to that same index. Naming any other secret is refused, the
  refusal is recorded in the audit log, and the message gives the `lakana secret bind` command
  to ask someone who manages secrets to run. Under the current runner, someone who also holds
  `DEPLOY` and `RUN` reads what the worker reads anyway (see the start of this page), so binding
  protects against an index manager redirecting a secret, not against code that runs in the
  worker. Changing the URL of an index that has a bound secret
  needs the right to manage secrets in that secret's environment too, because the URL is where
  the secret goes.
- Anyone who can read an environment can list the indexes: `lakana index list` shows names and
  URLs, never a password.

### What the URL may look like

Lakana stores the URL in one spelling (host in lower case, no trailing slash, no `:443`), and
refuses one it could not keep exactly. A refused URL gets a message that says which rule it broke.
In short:

- It starts with `https://`. `http://` is accepted only for `localhost` and loopback addresses.
- It has no user name and no password in it, no `?` and no `#`: the credential goes into the
  secret.
- The host is a plain DNS name or an IP address in its canonical form. A host name with an
  underscore is not supported.
- The path is made of letters, digits and `- . _ ~ + @`. A path with a percent sign or a space is
  not supported.
- It is not PyPI's own address (`pypi.org`, `files.pythonhosted.org`): PyPI takes no credential
  and needs no registration. A lock may name `https://pypi.org/simple` as it is.
- TestPyPI (`https://test.pypi.org/simple`) is an index of its own. Register it **without**
  `--credential-secret`: its downloads need no credential, and Lakana refuses a credential for it.

Two forms that many indexes use work as they are: **GitLab** with the numeric project ID
(`https://gitlab.example.org/api/v4/projects/1234/packages/pypi/simple`, not the project's path,
whose `%2F` is a percent sign) and **devpi** (`https://devpi.example.org/team/prod/+simple`, that is `USER/INDEX/+simple`). The
whole rule, with the table of fields, is in the reference:
[Package indexes](../reference/package-indexes.md).

## 2. Name the index in the project

In `pyproject.toml`, declare the index with the registered URL (`lakana index list` shows it),
mark it `explicit`, and say which packages come from it. With the
dependency on `lakana` of [Projects and deploys](projects.md) ("Python assets", step 4), which
comes from the public index, the whole file reads:

```toml
[project]
name = "analytics"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["lakana[assets]==<version>", "team-utils==1.4.0"]

[[tool.uv.index]]
name = "team"
url = "https://pkgs.example.org/simple"
explicit = true

[tool.uv.sources]
team-utils = { index = "team" }
```

`<version>` is the version of your instance, as in that step. The `team`
index and the `team-utils` lines are the new ones. Every other package comes from the public
package index, as it does for any project.

`explicit = true` means that only the packages you name under `[tool.uv.sources]` come from this
index, and that those packages come from nowhere else. Without it, `uv lock` could take a package
of the same name from the public index, and somebody else could publish one there. Lakana refuses
a registered index that is declared without it. If your private package depends on another
private package, that one needs its own line under `[tool.uv.sources]`.

## 3. Lock on your machine, commit, deploy

`uv lock` runs on your machine with **your own** credential for the index. uv reads it from two
variables named after the index (`TEAM` for an index named `team`):

<!-- not tested: needs a package index that asks for a password, which no example can reach; six tests of packages/lakana-server/tests/test_python_assets_integration.py run the effect of these commands end to end -->
```bash
export UV_INDEX_TEAM_USERNAME=your-user
export UV_INDEX_TEAM_PASSWORD=your-password
uv lock
```

`uv.lock` records the index's URL and each file's hash, and no credential. Commit
`pyproject.toml` and `uv.lock`, then deploy and run as always:

<!-- not tested: needs a package index that asks for a password, which no example can reach; six tests of packages/lakana-server/tests/test_python_assets_integration.py run the effect of these commands end to end -->
```bash
git add pyproject.toml uv.lock && git commit -m "Use team-utils"
lakana deploy --env dev
lakana run --env dev
```

The first task that needs the project's environment builds it. The worker reads the secret at
that moment, gives the credential to `uv` for the registered index's host and port only, and
removes it from everything `uv` prints. A later task uses the environment that was built and
reads no secret. The package indexes a deployed project uses are shown by `project-version show`,
one per line under `package indexes:` (`  - team`):

<!-- not tested: ID is a placeholder for a project_version_id, which `lakana deployments` prints for a deployed project -->
```bash
lakana deployments --env dev
lakana project-version show ID --env dev
```

## What the lock and the worker accept

- **The lock names indexes, not download hosts.** A lock may name the public index, the folder
  `wheelhouse`, the operator's extra index and the registered indexes, and no other index. The
  check looks at the index a package comes from. A file's address in the lock may point to another
  host (many indexes keep their files on a storage service): uv fetches it from there, and the
  credential is **not** sent to that host. Keep the files on the index's own host and port if
  they need the password.
- **Every file of an index is pinned by its hash, and only by an `http` or `https` URL.** `uv lock`
  writes both; a lock edited by hand that drops a hash, keeps only an MD5 one, or names a `file:`
  URL is refused at deploy and before every run. The hash is what makes another download host
  safe: the bytes cannot differ from what you locked. Which hosts a build may reach at all is set by
  the operator's network rules for tasks, not by the lock. Files in `wheelhouse` are named by their path and
  need no hash: the deployed commit fixes their bytes.
- **The worker builds without your `[tool.uv]` settings.** It runs `uv sync --frozen --no-config`.
  Ignored on the worker: the settings under `[tool.uv]` (for example `no-build`,
  `no-binary-package`, `required-version`, `compile-bytecode`, proxy and TLS settings) and any
  `uv.toml`: one at the root of the project is refused at deploy, any other (a system-level one on
  the worker's host) is ignored. Still read: the lock, and the index definitions in
  `[[tool.uv.index]]`. The settings still apply when you run `uv lock` on your machine. Do not put
  a user name or password into the URL of an index in `pyproject.toml`: the file is committed, and
  the credential of a registered index belongs in its secret.
- **A package comes from a registered index, from `wheelhouse` or from the public index.** A
  package from git, from a single file URL or from another folder is refused at deploy.

## Wheels only

When a project uses an index with a credential, the worker installs **wheels only**, for every
package of the project. Building a package from its source runs that package's build script
inside the build, where the credential is in reach. A package without a wheel for the worker's
platform and Python fails the build. In this example, `sdonly` is a package that the index offers only as a source
distribution:

<!-- output -->
```text
environment build failed: sdonly has no wheel for this platform and Python; publish a wheel to your index, or remove the dependency. A project that uses a package index with a credential installs wheels only.
```

For a pure Python package, `uv build --wheel` creates the wheel; publish it next to the source
distribution.

## Change the password, or remove it

Set the secret again in each environment. Nothing is deployed again:

<!-- source: docs/examples/getting-started/run.sh#private-index-rotate -->
```bash
lakana secret rotate team-index --env dev --values-stdin < credential.json
```

An environment that was built keeps working without the secret. The next build uses the new
value. The secret stays bound to the index, and no other index or repository can name it unless
someone who manages secrets moves it with `lakana secret bind`. To take the credential off an
index, an administrator clears the secret's name:

<!-- source: docs/examples/getting-started/run.sh#private-index-clear -->
```bash
lakana index update team --clear credential_secret
```

A worker keeps the files it downloaded in a cache of its own. When the index marks its files as
fresh, as real indexes do, a build with a rotated credential, or with none, installs a wheel that
an earlier build on the same worker fetched without asking the index again. A deploy still needs
the secret to exist. A rotated or removed credential is noticed at the next download, not at the
next build.

## What can go wrong

| Message begins | Cause | What to do |
|---|---|---|
| `uv.lock: takes … from the package index …, which is not registered on this instance` | The lock names an index nobody registered, or its URL is not the registered one. Two URLs are the same index when they differ only in the letter case of the host, in a default port that is written out (`:443`) and in trailing slashes; a path that differs, in letter case too, is another index. | Ask an administrator to run `lakana index add`, or use the URL that `lakana index list` shows and run `uv lock` again. |
| `uv.lock: takes … from the package index …, which is not a registered package index, and this URL cannot be registered as written: …` | The index's URL breaks a rule of "What the URL may look like". | Take the package from a registered index, or from an index with a plain `https` URL. |
| `uv.lock: takes … from the local folder …` or `… from a file outside the folder `wheelhouse`` | A package comes from a folder other than `wheelhouse`, or a file of `wheelhouse` has a path that leaves it. | Put the files into `wheelhouse`, or take the package from a package index, then run `uv lock`. |
| `uv.lock: takes … from a git repository, which is not supported` (or `… from a direct URL …`) | A dependency comes from git or from a single file URL. | Publish it to a package index and register that index. |
| `pyproject.toml: the index … is a registered package index and needs `explicit = true`` | The index is declared without `explicit = true`. | Add it and the `[tool.uv.sources]` lines, run `uv lock`, commit, deploy. |
| `pyproject.toml: the index named … points at …, but the package index registered as …` | Your index has the name of a registered index and another URL. Checked at a deploy only, and not for an index whose `url` is a folder, such as `wheelhouse`. | Use the registered URL or another name. |
| `pyproject.toml: `index-url` names the registered package index …` (or `extra-index-url`) | A registered index is set as `index-url` or `extra-index-url`. | Declare it as a `[[tool.uv.index]]` table with `explicit = true` instead. |
| `uv.lock: the package index … needs the secret … which does not exist` | The environment has no secret of that name. | Someone who manages secrets creates it with its use: `lakana secret create NAME --env ENV --index INDEX --values-stdin`. A secret created without `--index` is not bound, and the next build is refused. |
| `uv.lock: Secret … is not bound to a use yet` or `… is bound to …, not to package index …` (at a run: `environment build failed: Secret …`) | The secret exists, but nobody bound it to this index: it is unbound, or it serves another index, a repository or its environment. Lakana sends a secret only to the use it is bound to. | Someone who manages secrets runs the command the message ends with: `lakana secret bind NAME --env ENV --index INDEX`. |
| `uv.lock: the secret … has no usable "password"` | The secret has no `password` value, or one that cannot be passed on (a NUL character). | Run the `lakana secret rotate` command the message shows, with a JSON object that has `username` and `password`. |
| `uv.lock: the secret … has a "username" (or a "password", or both) longer than 4096 characters` | The value the message names is longer than 4,096 characters. A deploy and a run refuse it, instead of failing the build with a message about the argument list. | Run the `lakana secret rotate` command the message shows, with a shorter value. |
| `uv.lock: the credentials of the package indexes this project uses, in environment …, are too long together` | The user names and passwords of all the indexes the project uses together are too long to pass to the build (about 100 KB once encoded). | Shorten them with `lakana secret rotate`, or use fewer indexes with a credential. |
| `uv.lock: the secret … holds a user name or password with a character that cannot be sent` | The user name or the password has a lone surrogate (a character that cannot be written as UTF-8). | Run the `lakana secret rotate` command the message shows, with values written in valid UTF-8. |
| `uv.lock: Secret … could not be decrypted` (or `Secret … is stored in the old format (1)`) | The instance's master key changed since the secret was stored, or the secret was stored by an older release. | Put the old master key back, or run the `lakana secret rotate` command the message shows. |
| `uv.lock: the package index … has a credential but no secrets master key is configured` | The instance has no `LAKANA_SECRETS__MASTER_KEY`. | Ask whoever runs the instance to set it for the server and the worker, as the message says. |
| `uv.lock: the package index … has a credential, but its host takes none` | The index's row holds a credential for a host that takes none (TestPyPI); possible only for a row that was changed in the database directly. No credential is sent. | An administrator removes the credential with the `lakana index update NAME --clear credential_secret` command the message shows. |
| `uv.lock: the stored URL of the package index … is not valid any more` | The index's row in the database holds a URL that Lakana would not accept today, so no credential is sent to it. This happens only to a row that was changed in the database directly. | An administrator sets the URL again with the `lakana index update NAME --url URL` command the message shows. |
| `environment build failed: The package index … refused the credential in secret …` | The index answered 401 or 403: the password is wrong or expired, or the account may not read the package. | Run the `lakana secret rotate` command the message shows, then start the run again; if the password was right, give the account the right to read the package on the index. No new deploy is needed. |
| `environment build failed: The package index … asked for a credential, and none is registered for it` | The index asks for a password and has no `credential_secret`. | An administrator creates the secret first, with `lakana secret create SECRET --env ENV --values-stdin`, then runs `lakana index update NAME --credential-secret SECRET`. Naming it binds it. |
| `environment build failed: uv exited with status 1` | Most often an index whose files sit on another host that asks for a credential: the credential is not sent there. The task's log shows uv's own error. | This layout is not supported: keep the index's files on its own host and port. |
| `environment build failed: … has no wheel for this platform and Python` | See "Wheels only". | Publish a wheel, or remove the dependency. |
| `The package index … already has this URL` or `A package index named … already exists` (from `lakana index add`) | A name or a URL can be registered once. | Use that index, or remove it first with the command the message shows. |
| `… The package indexes registered on this instance no longer match this project version` | An index was removed or its URL changed after the deploy. | Register it again and start the run again (no deploy is needed), or change the project and deploy again. |
| `… A package index is registered on this instance that the project declares in a way that is not allowed` | The project declares an index in `pyproject.toml` without `explicit = true`, or as `index-url` or `extra-index-url`, and the index was registered after the deploy. Registering it again does not help. | Declare the index as a `[[tool.uv.index]]` table with `explicit = true`, name its packages under `[tool.uv.sources]`, run `uv lock` and deploy again. |

At the time of a run, the messages about the secret (missing, no usable password, a character that
cannot be sent, too long, no master key, cannot be decrypted) and the two about a stored row (the URL is not valid any more; a credential for a host that takes none) begin with
`environment build failed:` instead of `uv.lock:`, and mean the same.

The first failure of a deploy looks like this:

<!-- output -->
```text
uv.lock: the package index 'team' needs the secret 'team-index' in environment 'dev', which does not exist. Create it with `lakana secret create team-index --env dev --index team --values-stdin` (a JSON object with "username" and "password")
```

And a wrong password, in `lakana runs show`:

<!-- output -->
```text
environment build failed: The package index 'team' refused the credential in secret 'team-index' of environment 'dev'. Set it again with `lakana secret rotate team-index --env dev --values-stdin`. If the password is right, check that its account may read the package: the index also answers 403 when the account may not read a package or file.
```

An index that cannot be reached, or that answers with a server error, fails the task like an
unreachable public index does, and the task is tried again until its retries run out (with
`retries: 0` in `lakana.yaml` it fails at the first attempt).

## What this does and does not protect

- A secret is bound to the one index it serves. Someone who may register indexes
  (`MANAGE_PACKAGE_INDEX`) cannot redirect another secret to a host of their choice: they can name
  only a secret already bound to that index, and they cannot change the URL of an index whose
  secret is bound unless they also manage secrets in its environment. A refusal is recorded in
  the audit log as `AUTHORIZATION_DENIED`. This stops the redirection by an index manager. It
  does not stop code that runs in the worker under the current runner, which reads what the
  worker reads.
- The credential is offered only to the host and port of the registered URL. A project that
  points the index's name at another host gets no credential there.
- The index's files must be on the same host and port as the index. A file that the index
  serves from another host is fetched without the credential.
- Under the current runner, everyone who may deploy Python code to the instance can read the
  credential, because a task runs as the worker's operating-system user (see the start of this page).
- A private package runs inside a task like every dependency does, with what the task may read
  and write. Register only an index whose packages you trust.
- A wheel that a worker downloaded once stays in that worker's download cache. Another project
  whose lock names the same file and hash gets it from there without a request to the index, if
  its environment has the secret that the index names. Lakana checks the lock and the secret; it
  does not ask the index again who may read a package.
- An author who locks a public package by mistake still ships it, as with any dependency.
  `explicit = true` prevents that for the packages you name.

The exact fields, commands and routes are in the reference:
[Package indexes](../reference/package-indexes.md).
