# Package indexes

A package index is a server that Python packages are installed from, such as a company's
own package server or a code host's. An administrator registers an index once, for the whole
instance, with a name, a URL and, if the index asks for a user name and password, the name of a
secret that holds them. The secret is created in each environment whose projects use the index,
so the credential can differ between `dev` and `prod`. A project's `uv.lock` may name the URLs of
registered indexes, and no others (see "Sources a lock may name").

## The record

| Field | Type | Meaning |
|---|---|---|
| `id` | UUID | The index's identifier. |
| `name` | text | The name you use in commands. Unique. Never changes. |
| `url` | text | The index's address, in the stored form described below. Unique. |
| `credential_secret` | text or `null` | The name of the secret that holds `username` and `password`, or `null` for an index that asks for none. Only the name is stored here. |
| `created_by` | UUID or `null` | The principal that registered it. |
| `created_at` | time | When it was registered. |
| `updated_at` | time | When it was last changed. |

- The **name** follows the rules of a repository name: no `/`, no `.`, at most 255 characters.
- The **URL** is at most 1024 characters of printable ASCII, with no backslash, and starts with
  `https://`. `http://` is accepted only for `localhost` and loopback addresses. It has no user
  name, no password, no `?` and no `#`: put the credential in the secret.
- The **host** is a plain DNS name (letters, digits and hyphens between dots, in lower case once
  stored; an international name in its `xn--` form) or an IP address written in its canonical
  form (an IPv4 address as four decimal numbers, an IPv6 address in brackets in its shortest
  form). A host name with an underscore is not supported. An international host name is written in its `xn--` form;
  Lakana does not check that form's validity, so an invalid one is refused later, when uv builds. A port is between 1 and 65535.
- The **path** is made of segments of letters, digits and `- . _ ~ + @`, with no empty segment
  and no `.` or `..` segment. A path with a percent sign or a space is not supported: use a URL
  whose path has none.
- The **host** may not be one of six hosts of the public index (`pypi.org`, `www.pypi.org`,
  `upload.pypi.org`, `pypi.python.org`, `files.pythonhosted.org`,
  `test-files.pythonhosted.org`), with or without a credential: PyPI never receives one, and a
  lock may name `https://pypi.org/simple` without registration.
- **TestPyPI** (`test.pypi.org`) may be registered, but only without a `credential_secret`: its
  downloads need none. Registering it with one, or adding one later, is refused with the message
  `The package index URL is TestPyPI's address. TestPyPI needs no credential for downloads;
  register it without one (leave out --credential-secret).` So none of the seven hosts ever
  receives a credential.
- The URL and the credential are checked together whenever either changes, and again by the
  worker before it reads a secret. A stored URL that fails the check then (possible only for a
  row changed in the database directly) stops the deploy or the build with a message that
  contains `the stored URL of the package index`, and no credential is sent.
- The URL is stored with scheme and host in lower case, without the default port (`:443`) and
  without trailing slashes (one or several), so `https://Pkgs.Example.org:443/simple/` is stored as
  `https://pkgs.example.org/simple`. A name and a stored URL are each unique: registering a
  second spelling of a URL that is already registered is refused, and the message names the index
  that has it.
- `credential_secret` is the name of a secret: 1 to 255 characters, with no space, control
  character, `:` or bidirectional control character (such as U+202E), or empty. A repository's
  `credential_secret` and a deploy key's `--secret` follow the same rule.

Two forms that many indexes use work as they are:

- **GitLab:** use the numeric project ID, `https://gitlab.example.org/api/v4/projects/1234/packages/pypi/simple`,
  not the project's path (its `%2F` is a percent sign).
- **devpi:** `https://devpi.example.org/team/prod/+simple` (the `+` is allowed).

A refused URL gets a message that says which rule it broke and what to write instead.

## The secret

An index with a `credential_secret` needs a secret of that name in each environment whose
projects use the index. The secret holds two keys:

| Key | Meaning |
|---|---|
| `username` | The user name. Optional: when it is absent, an empty user name is sent. |
| `password` | The password or token. Required. |

Every other character is allowed in both, except a NUL character and a character that cannot be
written as UTF-8 (a lone surrogate): neither can be passed on. A secret with a NUL character counts
as having no usable password; one with a lone surrogate is refused with its own message. The user name and the password are at most 4,096 characters each; a longer one is refused, at a deploy and before a run, with its own message. A user name of at least 8 characters is redacted from uv's output with the password, except the fixed names many services prescribe (`__token__`, `gitlab-ci-token`, `x-access-token`, …), which are not secret. The secret is created with `lakana secret create NAME
--env ENV --values-stdin` and set again with `lakana secret rotate NAME --env ENV
--values-stdin`; both read a JSON object from standard input.

A deploy whose lock uses a registered index with a `credential_secret` stops unless that secret
exists in the target environment and holds a usable `password`; the message says which command
creates or sets it again. A deploy records the names of the registered indexes its lock uses, and
`lakana project-version show` prints them under a line `package indexes:`, one per line as `  - alpha`
and `  - team`, with control characters written as escapes (the first 20, then a line
`and N more: use --output json`); a project version that uses none prints no
such line.

## Sources a lock may name

At deploy, and again before every execution, Lakana reads the project's `uv.lock` and refuses it
when a package comes from a source it does not know. Two URLs name the same index when they
differ only in the letter case of scheme and host, in a default port that is written out and in
trailing slashes.

| Source in the lock | Verdict |
|---|---|
| The registry `https://pypi.org/simple` | allowed |
| The folder `wheelhouse` at the root of the project | allowed |
| The index the operator set in `LAKANA_RUN__INDEX_URL` | allowed, when it is set |
| A registered index's URL | allowed |
| Any other index URL | refused; the message names the package and the URL and says to ask an administrator to register the index. A URL that cannot be registered as written (`http` for a remote host, `%` in the path, a user name, ...) gets no command: the message says why, and to take the package from a registered index |
| A file of the folder `wheelhouse` whose path leaves it (`..`, a leading `/`, a backslash) | refused; packages in `wheelhouse` must be files inside that folder |
| Another local folder | refused |
| `git`, a direct `url` | refused; the message says to publish the package to a package index and register that index |
| `path`, `directory`, `editable`, `virtual` (other than the project itself) | refused |

Three rules apply to `pyproject.toml`:

- A `[[tool.uv.index]]` table whose URL is a registered index's (a user name in the URL does not
  change that) must have `explicit = true`, and
  the packages that come from it are named under `[tool.uv.sources]`. This stops a public package
  of the same name from replacing yours.
- A `[[tool.uv.index]]` table with the name of a registered index and another URL is refused.
  Use the registered URL, or another name. This rule is checked at a deploy only, not before an
  execution: registering a name later does not stop a project that is deployed. It does not
  apply to a table whose `url` is a local folder, such as the `lakana` index that points at
  `wheelhouse`.
- `index-url` and `extra-index-url` may not name a registered index. Declare it as a
  `[[tool.uv.index]]` table instead.

## What the worker does with the credential

The worker reads the secret only when it builds an environment, and a deploy only checks that the
secret exists and holds a usable `password`. It gives the credential to `uv` as an index the worker
defines itself, bound to the scheme, host and port of the registered URL, so another host, or
another port of the same host, never receives it. A build that uses a credential runs `uv sync`
with `--no-build`, so only wheels are installed and no package's build script runs. The credential
is removed from everything `uv` prints: the password and `user:password` as given and percent-encoded, and the basic-auth encoding of the pair. It is not in the project's
directory, in the built environment, in uv's cache or in the task's process.

## Commands

`--output json` (a global option of `lakana`, placed before the command) prints JSON instead of
text.

| Command | What it does | Needs |
|---|---|---|
| `lakana index add NAME --url URL [--credential-secret SECRET]` | Registers an index and prints `Registered package index NAME`. With `--credential-secret` it binds each unbound secret of that name to the index, in every environment that has one, which needs `MANAGE_SECRET` in each (otherwise the command is refused and binds nothing); a secret that is bound elsewhere, or that its environment uses for storage or webhooks, is refused. With text output it also prints, on standard error, how to finish: the `lakana secret create ... --index NAME` command for an environment where the secret does not exist yet, and the `lakana secret bind` command for one where it is not bound to this index. The hint is printed whenever a credential secret is named, and the command looks nothing up. | `MANAGE_PACKAGE_INDEX`; `MANAGE_SECRET` in an environment to bind its unbound secret |
| `lakana index list` | Lists the indexes: name, URL and secret name. A narrow window leaves the secret name out and says so. | `READ_ENVIRONMENT` on any environment |
| `lakana index show NAME` | Shows one index with all its fields. | `READ_ENVIRONMENT` on any environment |
| `lakana index update NAME [--url URL] [--credential-secret SECRET] [--clear credential_secret]` | Changes the options given; `--clear credential_secret` removes the secret. The name cannot be changed, and the URL cannot be cleared. Prints `Updated package index NAME`. A new URL stops the deployed projects whose lock names the old one, as `remove` does, until the old URL is registered again or the projects are locked with the new one and deployed again. With a new URL, and with a name in `--credential-secret`, the secrets bound to the index are checked. | `MANAGE_PACKAGE_INDEX`; also `MANAGE_SECRET` in the environment of each secret bound to the index, for a new URL |
| `lakana index remove NAME [--yes]` | Deletes the index after asking, and prints `Deleted package index NAME`. Projects whose lock names it stop running until it is registered again. Without a terminal, `--yes` is required. | `MANAGE_PACKAGE_INDEX` |

## SDK

`client.package_indexes` has these methods. The class of a result is
`lakana._models.PackageIndex` (the package does not export it by name):

| Method | Does |
|---|---|
| `create(name, url, *, credential_secret=None)` | Registers an index and returns it. |
| `list()` | Returns every index, sorted by name. |
| `get(name)` | Returns one index; raises `NotFound` for an unknown name. |
| `update(name, **changes)` | Changes `url` and `credential_secret`; `credential_secret=None` clears the secret. Any other keyword raises `TypeError` before a request is sent. |
| `delete(name)` | Deletes the index. |

A `PackageIndex` has the fields of the record: `id`, `name`, `url`, `credential_secret`,
`created_by`, `created_at` and `updated_at`.

```python
import lakana

with lakana.Client() as client:
    client.package_indexes.create(
        "team", "https://pkgs.example.org/simple", credential_secret="team-index"
    )
    for index in client.package_indexes.list():
        print(index.name, index.url, index.credential_secret)
```

## REST

Five routes under `/api/v1/package-indexes`. The CLI and the SDK use only these.

| Method and path | Body | Answer |
|---|---|---|
| `POST /api/v1/package-indexes` | `{"name", "url", "credential_secret"}`; the secret is optional | `201` and the index |
| `GET /api/v1/package-indexes` | | `200` and a list of indexes, sorted by name |
| `GET /api/v1/package-indexes/{name}` | | `200` and the index |
| `PATCH /api/v1/package-indexes/{name}` | `{"url"}` and `{"credential_secret"}`, either or both; `null` clears the secret | `200` and the index |
| `DELETE /api/v1/package-indexes/{name}` | | `204` |

Status codes: `401` without a credential; `403` without the permission; `404` for an unknown name
when the caller may read; `409` for a taken name or URL; `422` for an invalid field, an unknown
field, a `PATCH` that changes nothing, a `PATCH` that names the `name` or clears the `url`.

## Permissions and audit events

| Permission | Allows |
|---|---|
| `MANAGE_PACKAGE_INDEX` | Create, update and delete, and also list and show. It is an instance-wide permission. The `ADMIN` role has it, and it can be granted as a single action (see below). |
| `READ_ENVIRONMENT` on any environment | List and show, so that the author of a project sees which indexes exist and their URLs. |

A task token, the credential of a running task, is refused on every route, whatever its
principal may do.

**What a holder of `MANAGE_PACKAGE_INDEX` can do.** An administrator can grant the permission on
its own, with `lakana grant create` and the action `MANAGE_PACKAGE_INDEX` at the scope of the
instance. Its holder can register an index at any URL, but a secret serves one use only: a package
index, a repository, or its environment. A holder of `MANAGE_PACKAGE_INDEX` alone can name only a
secret that is already bound to that same index. Naming an unbound secret, one bound elsewhere,
or one its environment uses for storage or webhooks is refused with `403`; the refusal is
recorded as `AUTHORIZATION_DENIED` and the message names the exact `lakana secret bind` command
for someone who manages secrets to run. A holder of `MANAGE_SECRET` in the environment who names
an unbound secret binds it in the same step. Changing the URL of an index that has a bound
secret needs `MANAGE_SECRET` in that secret's environment, whatever credential the index names
at that moment, because the URL is where the secret goes. So a holder of `MANAGE_PACKAGE_INDEX`
cannot redirect the `password` of an environment's secret to a host of their choice. Grant the
permission only to people you would trust with the index's own credential. This stops the
redirection by an index manager, not code that runs in the worker: under the local runner a
person who may deploy and run Python code reads what the worker reads anyway (with the docker
runner a task holds no credential, see [Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md))
(see
[Isolation: one instance is one trust domain](../explanation/python-assets.md#isolation-one-instance-is-one-trust-domain)).
`MANAGE_REPOSITORY` follows the same rules for a repository: its holder can name only a secret
bound to that repository and cannot move its URL while a secret is bound without
`MANAGE_SECRET`. A holder without `DEPLOY` can still stop projects: a changed URL and a removed
index both stop the deployed projects that use the index.

Three audit events record the changes. None holds a credential: only the name of the secret.

| Event | When | Detail |
|---|---|---|
| `PACKAGE_INDEX_CREATED` | An index is registered. | `name`, `url`, `credential_secret` |
| `PACKAGE_INDEX_UPDATED` | An index is changed. An update that gives the values the index already has (a URL in another spelling counts as the same) changes nothing and records no event. | `name`, `changed` (the names of the fields whose value changed), `url` and `credential_secret` after the change, `previous` (the value each changed field had before) |
| `PACKAGE_INDEX_DELETED` | An index is deleted. | `name`, `url` |
