# Releases

Referenced from `CLAUDE.md` when you are cutting a release. **Never tag by hand** — every
version comes from git tags via `hatch-vcs`, and hand-tagging bypasses release-please's
changelog and PR trail.

## Conventional Commits, Branch, and Changelog

PRs are squash-merged: the PR title becomes the commit subject and the PR description the
commit body (repository setting: squash title = PR title, message = PR body). That commit
is all release-please reads, so `.github/workflows/conventional.yml` (the required
`conventional` check) enforces four things on every PR:

- **Title** — Conventional Commits, linted by commitlint with `commitlint.config.mjs`,
  which extends `@commitlint/config-conventional` (conventional-changelog's Conventional
  Commits preset) with Lakana's type and scope lists. The CLI versions are pinned in the
  workflow; Node is a CI-only tool, never a project dependency.
- **Branch** — Conventional Branch, `<type>/<lowercase-description>`
  (`scripts/check_conventional.py`); `release-please--*` and `dependabot/*` are exempt.
- **Footers** — the description's final paragraph carries one `Closes #N` per issue (a
  keyword followed by several issues closes only the first) and, when the title has `!`,
  `BREAKING CHANGE: <what changed and how to migrate>`. HTML comments and Markdown code (the PR template's
  guidance, quoted examples) are ignored.
- **The platform REST API** — a breaking change of it needs `!` in the title
  (`scripts/openapi_diff.py`, next section).

## The REST API contract

The platform REST API (every route under `/api/`) is a versioned public contract; the user page is
`docs/site/reference/rest-api.md`, and the Iceberg REST catalog (`/v1/`) is not part of it. The
rule for agents:

- **The snapshot is generated, never edited.** `docs/site/reference/openapi.json` is written by
  `uv run python scripts/generate_reference.py` from the platform routers in
  `lakana_server.app.ROUTERS` (without the Iceberg router), with `info.version` fixed to
  `unreleased`. Any change to a route, a parameter, a request or response model (a field, a type,
  a default, a `Literal`, a constraint, a docstring) changes it: regenerate and commit it in the
  same change. CI's `docs` job runs `--check`, and `scripts/tests/test_generate_reference.py`
  checks it and that an instance's `/openapi.json` serves the same platform routes.
- **A breaking difference needs `!`.** The `conventional` check compares the snapshot of the merge
  commit with its first parent's (the base branch) using `scripts/openapi_diff.py
  --require-breaking-title`. `scripts/openapi_diff.py`'s docstring lists what is breaking and
  what is additive; its tests have a case for each. Run it locally against `main` before opening
  the PR, for example:

  ```bash
  git show main:docs/site/reference/openapi.json > ~/.cache/openapi-main.json
  python3 scripts/openapi_diff.py ~/.cache/openapi-main.json docs/site/reference/openapi.json
  ```

  A breaking change before v1.0.0 needs `!`, the `BREAKING CHANGE:` footer and an upgrade note in
  `docs/site/explanation/upgrades.md`. From v1.0.0 a breaking change lands only in a major
  release: deprecate instead.
- **Deprecate with `lakana_server.deprecation.deprecated`.** `@router.get(path,
  **deprecated(since=date(...), sunset=date(...)))` marks the route `deprecated: true` in the
  document (with `x-deprecated-since` and `x-sunset`), and `DeprecationHeaders` (installed by
  `create_app`) then sends `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) on its answers. Never
  write `deprecated=True` on a route by itself: `test_every_deprecated_route_says_since_when`
  fails. A field or a parameter is deprecated with pydantic's / FastAPI's `deprecated=True`; it
  gets no header. A deprecated route, field or parameter keeps working for at least two minor
  releases and three months, whichever is longer, and is removed only in a major release; say so
  in the changelog and the upgrade note of the release that deprecates it.
- **The release attaches it.** The `openapi` job of `release.yml` writes the tag's version into
  `info.version` of the committed snapshot and `sbom-release` uploads it as `lakana-openapi.json`
  next to the SBOM.

**Changelog sections** follow the Conventional Commits preset, set explicitly as
`changelog-sections` in `release-please-config.json` (the same `{type, section, hidden}`
schema conventional-changelog uses):

| Type | Section | In `CHANGELOG.md` |
|---|---|---|
| `feat` | Features | yes |
| `fix` | Bug Fixes | yes |
| `perf` | Performance Improvements | yes |
| `revert` | Reverts | yes |
| `docs` | Documentation | yes |
| `deps` | Dependencies (Dependabot's `uv` updates) | yes |
| `refactor`, `test`, `ci`, `build`, `chore` | — | hidden |

A breaking change is listed under **⚠ BREAKING CHANGES** with its footer text, so write
the footer for the reader who has to migrate. Dependabot's GitHub Actions updates use the
`ci` prefix and stay hidden. release-please titles its own PR `chore(release): release
<version>` (`pull-request-title-pattern`), which passes the same lint.

We use release-please rather than conventional-changelog's own CLI: it already parses
Conventional Commits, computes the bump, and writes the changelog; running both would
produce two competing changelogs.

### Version bumps

By release-please's default rules:

- `fix:` → patch bump
- `feat:` → minor bump
- a `!` after the type, or a `BREAKING CHANGE:` footer → major bump

The design intent is that **pre-1.0, breaking changes bump the
minor** rather than jumping to `1.0.0` — releasing fast without prematurely committing to a
stability promise. That behaviour requires release-please's `bump-minor-pre-major` option,
and `release-please-config.json` now sets it (`"bump-minor-pre-major": true`) — so a
`BREAKING CHANGE:`/`!` commit bumps the minor version pre-1.0 as intended, not straight to
`1.0.0`. One caveat still worth knowing: `.release-please-manifest.json` starts at `0.0.0`,
and release-please has a documented quirk where pre-major options can be silently ignored
when the manifest starts exactly at `0.0.0` (working around it means bumping the manifest to
`0.0.1`). Confirm the first release actually respects `bump-minor-pre-major` rather than
assuming the config setting alone guarantees it.

## How the first public release happens

The public repository starts with one commit and no tags. Nothing is tagged by hand:

- `.release-please-manifest.json` says `0.0.0`, so release-please finds no previous release.
- `"initial-version": "0.12.0"` in `release-please-config.json` is what it then proposes. It is
  read only while no release exists, so it needs no edit afterwards.
- Do **not** set `bootstrap-sha`, `last-release-sha` or `release-as`. The first two stop the
  commit walk at that commit and exclude it, which leaves no commits and no release PR;
  `release-as` would propose 0.12.0 again for every later release.
- The first commit must be `feat:` or `fix:`; a hidden type such as `chore:` yields no release
  PR. Its subject is the one bullet in the first changelog entry.
- Merging the release PR creates the tag `v0.12.0` and runs the publish jobs.

### The release-please token

The release PR must run CI, and a pull request opened with the workflow's own `GITHUB_TOKEN`
starts no workflow runs. The repository setting "Allow GitHub Actions to create and approve
pull requests" is therefore **off**, and `release.yml` passes a fine-grained personal access
token to the action as `token: ${{ secrets.RELEASE_PLEASE_TOKEN }}`.

- Scope: only the repository `lakana-labs/lakana`, with the repository permissions *Contents:
  read and write*, *Pull requests: read and write* and *Issues: read and write* (release-please
  creates its `autorelease` labels), nothing else.
- Create the token and store it as the Actions secret `RELEASE_PLEASE_TOKEN` **before the first
  push to `main`**: without it the `release-please` job fails on that push and no release PR
  opens. Set an expiry and renew it before then.
- If the token is missing or expired, the `release-please` job fails at once with an
  authentication error and nothing is released; create a new token and update the secret.

## The release-please PR flow

`release.yml` runs `googleapis/release-please-action` on every push to `main`. It keeps a
single open "release PR" up to date: as conventional-commit PRs land, it recomputes the next
version and regenerates `CHANGELOG.md` inside that PR, but nothing is tagged or published
yet. **Merging that release PR** is the release act — the action then tags the resulting
commit and sets `released: true`/`tag_name` as job outputs, which gate the `build` and
`sbom` jobs in the same workflow run — `publish-core`, `publish-runtime`, and `publish-sdk`
each run only once the job ahead of them (ultimately `build`) has succeeded, so they need
no `if` of their own; see "Publishing in dependency order" below.

**Upgrade notes.** `docs/site/explanation/upgrades.md` keeps a section "Notes for the next
release" for changes that land before the version is known (release-please decides the version
in the release PR). Before you merge the release PR, rename that heading to "Notes for X.Y.Z",
add the release to the table "Upgrade notes of the last releases", and fix the links to the
heading. The first such note is the rename of the deployed snapshot to "project version".

`release-type: "simple"` plus `include-component-in-tag: false` in
`release-please-config.json` means one version for the whole repo, not independent versions
per package — consistent with all four packages reading their version from the same git
tag via `hatch-vcs` (each package's `pyproject.toml` sets
`[tool.hatch.version] source = "vcs"` with `raw-options = { root = "../.." }`, pointing at
the repo root, not the package directory).

`package-name` (`lakana`) never reaches the tag. Because the component is left out of tags,
release-please matches the last release by path (`"."` in `.release-please-manifest.json`)
and version alone, so the previous release is whichever tag the manifest's version names. The name only appears in the release PR's branch,
`release-please--branches--main--components--lakana`.

`release-type: "simple"` also means release-please creates and maintains a root
`version.txt` file as part of the release PR — that's its own bookkeeping mechanism for
tracking the current version between releases, unrelated to `hatch-vcs`. **Nothing in this
repository reads `version.txt`**; the packages' actual versions still come from git tags via
`hatch-vcs`, as above. `CLAUDE.md`'s "never hand-edit a version" rule is about that
`hatch-vcs`-derived version, not this file — `version.txt` is meant to be written by
release-please's own PRs, so don't read its appearance in the first release PR as a
contradiction of that rule.

## Trusted Publishing

Each publish job authenticates to PyPI via **Trusted Publishing (OIDC)** —
`permissions: id-token: write`, no long-lived API token in repository secrets — and each of
the four packages has its **own** GitHub Environment: `pypi-lakana`, `pypi-lakana-core`,
`pypi-lakana-server`, `pypi-lakana-worker`. This isn't incidental granularity — PyPI
requires a pending trusted publisher to be unique on `(owner, repo, workflow, environment)`.
With one workflow (`release.yml`) publishing all four packages, the environment is the only
axis left to disambiguate, so each package needs its own. The jobs publish to production PyPI
(the action's default repository); there is no TestPyPI path in the workflow.

The trusted publisher of each project on pypi.org is: owner `lakana-labs`, repository `lakana`,
workflow `release.yml`, and the environment of that package from the list above.

**Adding a package is two steps, and the YAML is only the first.** A new package publishes
nothing until the matching GitHub Environment exists under Settings → Environments *and*
has a (pending) trusted publisher registered for it on pypi.org; the job otherwise fails at
authentication. All four packages (`lakana`, `lakana-core`, `lakana-server`, `lakana-worker`) need
both. Register the four as pending publishers before the first release (account settings,
"Publishing"); a pending publisher becomes a normal one when its project's first upload succeeds.

## One approval before anything is published

The job **`approve`** is the release's only gate. It `needs` `release-please` and `build`,
runs in the environment `release-approval`, has `permissions: {}`, checks nothing out and runs
one `echo`. The run stops there until the maintainer approves it (Actions run page, "Review
deployments"). Every job that publishes or signs depends on it, directly or through its
`needs`: the four PyPI jobs, `publish-image` and `attest-image`, `attest-dist` and
`provenance-release`, `openapi` and `sbom-release`, `pages` and `pages-deploy`. `release-please` (tag and
GitHub release), `build` and `sbom` run before the gate: they upload nothing but workflow
artifacts (`dist`, the SBOM), which logged-in users can download from a public repository. The
two smoke jobs follow the publish jobs, so they run after it too.
**Rejecting publishes nothing**: PyPI, the registry, the release page and the docs site stay
as they were. The tag and the GitHub release already exist. To retry, use **"Re-run failed jobs"** on the
rejected run, which asks for the approval again (GitHub allows it for 30 days). Never use
"Re-run all jobs": it re-runs `release-please`, which finds the release already tagged, so
every job is skipped and the run ends green with nothing published. A pending approval holds
the `release-${{ github.ref }}` concurrency group, so later pushes to `main` queue behind it:
approve or reject promptly.

`scripts/tests/test_workflows.py` pins the job (its full key set, so it can gain no
`continue-on-error`, permission or `uses:`) and that every job apart from `release-please`,
`build` and `sbom` reaches it through `needs` with no status function (`always()`, `cancelled()`,
`failure()`, `success()`, negated or not) in its `if`, and that `build` and `sbom` hold no write
permission and read no secret. A new job after the gate needs no test change; a new job before it
has to be added to the test's list on purpose.

**Maintainer setup, before the next release.** The workflow cannot create the environment's
protection, and GitHub creates a missing environment *without* a reviewer, which would let
the release through unapproved. Create `release-approval` under Settings > Environments with
the maintainer as required reviewer and the same branch/tag rule as the PyPI environments.
Do not tick "Prevent self-review": the maintainer is the only reviewer and usually merged the
release PR, so nobody could approve. "Allow administrators to bypass" stays on (the default),
which is acceptable for a sole maintainer. Then remove the required reviewer from `pypi-lakana`, which asked on its own until now; keep
its branch rule and that of the other three PyPI environments (the trusted publishers name
them).

## Publishing in dependency order

`release.yml`'s publish side is four jobs, not one matrix, because a failed upload must
never leave a live package whose pinned dependency is missing:

- **`build`** checks out the tag, runs `uv build --all-packages -o dist` once, verifies no
  wheel carries a local version segment (see below), and uploads `dist/` as a build
  artifact for the jobs that follow.
- **`publish-core`** needs `build` and `approve` and publishes `lakana-core` alone (`pypi-lakana-core`).
- **`publish-runtime`** needs `publish-core` and is a `fail-fast: false` matrix of
  `lakana-server` (`pypi-lakana-server`) and `lakana-worker` (`pypi-lakana-worker`) — both
  pin `lakana-core` (see below), so neither publishes until `lakana-core` already has.
- **`publish-sdk`** needs `publish-runtime` and publishes `lakana` (`pypi-lakana`) last. A
  job's `needs` waits on the *entire* needed job, every matrix leg included, so `publish-sdk`
  only runs once **both** `lakana-server` and `lakana-worker` have published — `lakana`'s `server`
  extra pins both, so publishing it any earlier could advertise a version of either that
  never landed.

Each publish job downloads the shared `dist/` artifact and copies only its own package's
wheel and sdist into a fresh `pkg-dist/` directory (matching each package's PyPI-normalized
name, e.g. `lakana_core-*`, so `lakana-*` cannot accidentally sweep up `lakana_core-*`), then
passes `packages-dir: pkg-dist` to `pypa/gh-action-pypi-publish` — each job's PyPI Trusted
Publisher is registered for exactly one project, so uploading another package's files
through it would fail authentication anyway. `permissions.id-token: write` lives only on
the three publish jobs (`publish-core`, `publish-runtime`, `publish-sdk`); `build` and
`sbom` need no OIDC token.

## The `lakana[server]` extra (M4c)

`pip install "lakana[server]"` pulls in `lakana-server` and `lakana-worker` alongside `lakana`
itself — everything `lakana server start --local` needs to run a complete instance on one
machine, with no Docker and no Postgres. `pip install lakana` alone stays SDK-and-CLI-only,
with neither package installed.

`packages/lakana/hatch_build.py` (`ServerExtraMetadataHook`, wired in via
`[tool.hatch.metadata.hooks.custom]`) pins both to **exactly** this build's own version —
`optional-dependencies.server = ["lakana-server==<version>", "lakana-worker==<version>"]` — set
from `metadata["version"]` after hatchling has already resolved it from the shared
`hatch-vcs` tag, so nothing here hand-edits a version (rule 3). This guarantees a matching
trio on an ordinary release, since all four packages share one version from one git tag.

The same hook declares `optional-dependencies.assets = ["polars>=1.30"]`: the extra a project
with Python assets installs in its task venv (`lakana.runner` needs polars; the SDK and CLI do
not, which is why the harness imports it inside functions).

**`lakana-core` is pinned too.** `packages/lakana-server/hatch_build.py`
and `packages/lakana-worker/hatch_build.py` each carry their own `CoreDependencyMetadataHook`,
the same shape as `ServerExtraMetadataHook`: `dependencies` is dynamic in each package's
`pyproject.toml`, the other dependencies (with their explanatory comments) live under
`[tool.hatch.metadata.hooks.custom]` as the hook's own config, and the hook appends
`lakana-core==<metadata["version"]>` to that list. Each hook lives inside its own package
root (not shared) because the sdist must be self-contained — a build from the sdist alone,
with no workspace around it, still needs to find its hook.

Combined with the publish ordering above, this closes the gap: `lakana-server`/`lakana-worker`
can never resolve a `lakana-core` other than the one just published ahead of them, and the
upload sequence guarantees that one exists before they do.

## Shipping the container image and verifying the release (M4d)

Four more `release.yml` jobs run after `publish-sdk`, each gated on the one before it so a
bad release never advances past the point that would have caught it:

- **`publish-image`** needs `publish-sdk` (as well as `release-please`, `build` and
  `approve`), not just the packages it
  bundles — this guarantees the image is only ever built from wheels that PyPI already
  has (see "Why untagged builds cannot publish" below), so a broken PyPI release never gets a
  matching `latest` container. It regenerates
  `dist/requirements.txt` (the shared `dist` artifact only carries wheels/sdists) and builds
  **multi-arch** (`linux/amd64,linux/arm64`, via `docker/setup-qemu-action` +
  `docker/setup-buildx-action`), pushing `ghcr.io/lakana-labs/lakana:<version>` — a public package.
- **`publish-task-images`** (one leg per supported Python) has the same needs as
  `publish-image`. It downloads the shared `dist` artifact and writes
  `dist/check-requirements.txt` (`uv export --frozen --package lakana --no-dev
  --no-emit-project`), so each task image's `/opt/lakana-check`, the connection check's
  environment, holds the release's own lakana wheel and its locked dependencies by hash. It pushes
  `ghcr.io/lakana-labs/lakana-task:<version>-py3.X`.
- **`compose-smoke-release`** needs `publish-image` and `publish-task-images` and runs
  `scripts/compose_smoke_test.sh` against the just-pushed tag and its task images (the second
  argument, `ghcr.io/lakana-labs/lakana-task:<version>`; CI's `compose-smoke` builds
  `lakana-task:ci-py3.14` from the checkout, and the script builds the 3.11 image the launcher's
  start-up probes need): `lakana bundle init` → `docker
  compose up -d --wait` → create/append/query → `docker compose down` → `up` again (proving
  a restart is a no-op and data survives) → query again. This is the same script CI's
  `compose-smoke` job runs against a locally built `lakana:ci` image on a PR that touches the
  bundle or any package's `src/` tree; here it runs against the real, pushed release image. This job builds its wheelhouse
  from the tag's checkout, so the checkout has `fetch-depth: 0`: hatch-vcs needs the tags to
  derive the same version the image was built with, and the script fails if the two differ.
- **`five-minute-release`** needs only `publish-sdk` (not the image jobs — it doesn't touch
  Docker) and runs `scripts/five_minute_test.sh` against **PyPI**, the same script CI's
  `five-minute` job runs against locally built wheels on every PR. PyPI's index can lag
  the upload by minutes, so `scripts/wait_for_pypi.py` first polls PyPI's JSON API and the simple index pip reads (every 30 s,
  for at most 15 minutes) until both show the version of all four packages, and fails with the missing
  names after that. Then, for the odd blip, it retries the whole script up to 5 times, 30s
  apart, on failure.

Both smoke scripts also deploy and run one Python asset (`silver.doubled`, reading
`bronze.example`) and an artifact pair (`models.doubler` stores a model, `gold.by_model` reads
it; `python_asset_project.sh ... artifact`) with no package index: `scripts/python_asset_project.sh` writes the project and
locks it with `uv lock --offline` against a wheel directory. `compose_smoke_test.sh` builds that
directory with `scripts/build_wheelhouse.sh` (or takes `LAKANA_WHEELHOUSE`) for the image's
Python (it asks the image which version that is), refuses a lakana wheel whose version differs
from the image's, and then checks in the container that `uv` is on the worker's PATH and the venv cache is `10001 700`.
`five_minute_test.sh` uses `pip download` with its own install arguments to fill the directory
(the same index traffic as the install). Build the image and the wheel directory from one
commit: the version carries the commit hash.

Both smoke scripts share one contract: install/build → `lakana bundle init` or `lakana server
start --local` → create a table → append → query, verified against a hard-coded, small
result set. Local verification of both runs on **every PR** that touches the relevant
surface, each against a 300s budget (`scripts/five_minute_test.sh`'s own `BUDGET` variable;
`compose_smoke_test.sh`'s `docker compose up -d --wait --wait-timeout 180`) — don't cite a
specific measured time as a promise, since CI runner speed varies.

## The SBOM

Two jobs of `release.yml` ship the SBOM (and `sbom-release` also attaches the OpenAPI document
that the read-only `openapi` job writes; see "The REST API contract"). `sbom` builds an SPDX software bill of materials of the
tagged tree with `anchore/sbom-action` (read-only token) and keeps it as the workflow artifact
`lakana-sbom.spdx.json`. `sbom-release` then downloads that artifact and attaches it to the
**GitHub release** of the tag, so it is found on the release page; it runs
`gh release upload "$TAG" … --clobber`, so a re-run replaces the file. `sbom-release` and
`provenance-release` (below) are the only jobs with `contents: write`: neither has a checkout or a
third-party action other than the artifact download, so no code that builds anything holds the
write token. The workflow's own `permissions` is `{}`; every
job names what it needs, and `release-please` needs nothing from `GITHUB_TOKEN` because it uses
`RELEASE_PLEASE_TOKEN`. `scripts/tests/test_workflows.py` pins this by effective permissions.

## The documentation site on GitHub Pages

The site is served at <https://lakana-labs.github.io/lakana/> (repository setting: Pages, source
"GitHub Actions"; the `github-pages` environment). Two jobs of `release.yml` publish it, and
only for a release (`released == 'true'`); the workflow has no manual trigger.

- `pages` checks out the **release tag** (`persist-credentials: false`), builds with the commands
  of CI's `docs` job (`docs_blocks.py`, `zensical build --strict`,
  `scripts/check_built_site.py site-build`) and uploads `site-build` with
  `actions/upload-pages-artifact`. It has `contents: read` only.
- `pages-deploy` runs `actions/deploy-pages` in the `github-pages` environment and holds only
  `pages: write` and `id-token: write`; it builds nothing.

So the site always describes the latest **release**: a documentation change merged to `main`
appears with the next release, not at once. Every page is under `/lakana/`, so `site_url` in
`mkdocs.yml` must stay `https://lakana-labs.github.io/lakana/`, and the built-site check refuses
a `href` or `src` that starts at the host root (`/assets/...`) outside that path.

To publish again (a failed deployment, a Pages outage), open the run of the `Release` workflow
for the **latest** release in the Actions tab, open its `pages` job and choose "Re-run this job"
(GitHub re-runs `pages-deploy`, which depends on it). Never choose "Re-run all jobs": it re-runs
release-please, which finds the release already made, so both Pages jobs are skipped and the run
turns green without deploying. "Re-run failed jobs" on `pages-deploy` alone works only while the
uploaded site is kept (one day). Re-running the run of an **older** release puts that older site
back. `scripts/tests/test_workflows.py` pins the trigger, the checkout
ref, the commands, the two jobs' permissions and the environment.

## Build provenance attestations

Every release is signed with GitHub's build provenance attestations (Sigstore, keyless:
`actions/attest-build-provenance`), so a user can check that a file or the image was built by
this repository's `release.yml` from the tagged commit.

- **`attest-dist`** needs `build` and `approve`, downloads the shared `dist` artifact and attests every wheel and
  sdist (`subject-path` lists `dist/*.whl` and `dist/*.tar.gz`). It has `id-token: write` and `attestations: write` and nothing
  else but `contents: read`. It also keeps the bundle as the workflow artifact `provenance`.
- **`provenance-release`** (like `sbom-release`: no checkout, only the artifact download and
  `gh release upload`, the only other job with `contents: write`) attaches the bundle to the
  GitHub release as `lakana-provenance.intoto.jsonl`, where OpenSSF Scorecard's Signed-Releases
  check looks for a signature.
- **`attest-image`** needs `publish-image`, which passes the pushed image's digest as a job
  output. It attests the image by digest (`subject-digest`, `push-to-registry: false`: `gh
  attestation verify` reads attestations from GitHub, so nothing is pushed to the registry). It
  is a job of its own with one step: `publish-image` runs several third-party actions, and any
  step of a job with `id-token: write` could ask for a signing certificate. `publish-image` has
  neither `id-token` nor `attestations`.
- No other job has `id-token` (the three PyPI publish jobs have it for Trusted Publishing) or
  `attestations` write; `scripts/tests/test_workflows.py` pins this by effective permissions.

How a user verifies (also in the user documentation, "Verify a download" in the Compose how-to):

```bash
gh attestation verify lakana-<version>-py3-none-any.whl --repo lakana-labs/lakana \
  --signer-workflow lakana-labs/lakana/.github/workflows/release.yml
gh attestation verify oci://ghcr.io/lakana-labs/lakana:<version> --repo lakana-labs/lakana \
  --signer-workflow lakana-labs/lakana/.github/workflows/release.yml
```

Check it after each release: the first release that has attestations is the first one cut after
this job landed; older releases have none, and `gh attestation verify` says so.

## Why untagged builds cannot publish

`hatch-vcs` derives a version from the most recent git tag plus a local segment when the
working tree is ahead of (or dirty relative to) that tag — e.g. this repository, which has
no tags yet, would report something illustrative like `0.1.dev15+g0051e86d9` for
`lakana-core` (the exact string is whatever commit you're on; it is not a fixed value and
this document isn't claiming it's the current one). PyPI
rejects uploads whose version carries a local segment (`+...`) — local versions
are explicitly disallowed by their upload API. `release.yml`'s `build` job checks for this
directly, once, right after building every package and before any publish job runs:

```bash
if compgen -G "dist/*+*.whl" > /dev/null; then
  echo "Local version segment present — PyPI will reject this." >&2
  exit 1
fi
```

Since the `build` job checks out `needs.release-please.outputs.tag` (the tag release-please
just created), a build from that job is exactly on a tag and carries no local segment. A
manual build from an arbitrary, untagged commit would carry one and fail this check — which
is the mechanism, not an incidental side effect, that keeps arbitrary commits from being
published: only a release-please-created tag produces a clean, publishable version. Every
publish job downloads that same build's artifact rather than rebuilding, so this check runs
exactly once per release, not once per package.
