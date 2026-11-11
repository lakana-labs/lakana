# Conventions

Referenced from `CLAUDE.md` when you are writing Python, or adding/upgrading a dependency.

## Numpydoc docstrings

Every module, class, function, and method needs a numpydoc-style docstring. There is no
`[tool.ruff.lint.pydocstyle]` table in `pyproject.toml` — it was deleted along with
`convention = "numpy"` when the project was set up. Setting a `convention` would make ruff *restrict*
the pydocstyle rule set to that convention's defaults, which would have left `D417`
(`undocumented-param`) off, since the numpy convention preset doesn't enable it. Instead,
`pyproject.toml`'s `[tool.ruff.lint]` selects the whole `"D"` group (plus preview `"DOC"`),
which turns on every pydocstyle rule including `D417`, and then carves out only the rules
that would fight numpydoc's format via an explicit `ignore` list — by rule *name*, not
code, per `RUF201` (see "Two ruff quirks worth knowing" below): things like
`incorrect-blank-line-before-class` (D203) and `multi-line-summary-first-line`/
`multi-line-summary-second-line` (D212/D213), which conflict with where numpydoc puts the
summary and the class docstring's blank line. It is not optional decoration: ruff checks
that a docstring exists, and the preview `DOC` rules cross-check it against the real
signature — a missing `Returns` section (`DOC201`) or an undocumented `Raises` (`DOC501`)
are lint failures too, not review comments.

**`D417`'s real precondition — read this before assuming a missing `Parameters` section is
caught.** `D417` (`undocumented-param`) only compares an *existing* `Parameters` section
against the function's signature: write a `Parameters` section that lists some but not all
parameters, and it flags the ones you left out. Omit the `Parameters` section entirely,
and `D417` has nothing to compare against, so it stays silent — a function can be
completely undocumented for its parameters and pass `D417` clean, as long as it never
starts a `Parameters` section at all. Nothing else in this rule set closes that gap: a
missing `Parameters` section is not itself checked by any rule here. That residual case is
on the author, not the gate — the docstring-exists check (`D103` etc.) only requires a
docstring to be present, not that it document parameters.

The opposite drift — a docstring that still documents a parameter a refactor removed from
the signature — is caught independently by `DOC102` (`docstring-extraneous-parameter`),
which does not depend on `D417` or on whether the `Parameters` section is complete:

```
docstring-extraneous-parameter: Documented parameter `b` is not in the function's signature
```

**Good:**

```python
def read_baseline(path: Path) -> float:
    """Read the recorded baseline.

    Parameters
    ----------
    path
        File holding the baseline percentage.

    Returns
    -------
    float
        The baseline, or ``0.0`` when the file does not exist.
    """
    if not path.exists():
        return 0.0
    return float(path.read_text().strip())
```

**Bad** — ruff rejects this for two independent reasons, confirmed by running
`uv run ruff check --no-cache` against the repo's real config:

```
undocumented-param: Missing argument description in the docstring for `read_baseline`: `verbose`
docstring-missing-returns: `return` is not documented in docstring
```

```python
def read_baseline(path: Path, verbose: bool) -> float:
    """Read the recorded baseline.

    Parameters
    ----------
    path
        File holding the baseline percentage.
    """
    if not path.exists():
        return 0.0
    return float(path.read_text().strip())
```

`D417` fires here only because the docstring *has* a `Parameters` section and `verbose` is
missing from it — deleting the whole `Parameters` section (as this example used to do)
would silence `D417` even though `path` and `verbose` would then both be undocumented; see
"`D417`'s real precondition" above. `DOC201` (`docstring-missing-returns`) fires
independently, for the missing `Returns` section.

Two exceptions, both in `pyproject.toml`: magic methods (`D105`) and `__init__` (`D107`,
documented on the class instead) don't need their own docstring. Test modules and test
functions are also exempt (`D100`/`D103` ignored under `**/tests/**`) — a test reads as its
own documentation.

`DOC502` (`docstring-extraneous-exception`) is deliberately ignored too. It would otherwise
flag a delegating wrapper — a function with no `raise` of its own that documents an
exception raised by something it calls — for documenting an exception "it doesn't raise."
`NamespacePath.parse` and `.child` (`packages/lakana-core/src/lakana_core/identifiers.py`) hit
exactly this shape: both document the `ValidationError` raised by `from_parts`, which they
call but don't raise from directly. `DOC501` still catches the opposite mistake, an
undocumented `Raises`, so nothing about a function's actual error behaviour goes unchecked;
only the false positive on delegation is turned off, so a wrapper can honestly document the
exception its callee raises. Decided when the project was set up.

## Inline comments: earn their place, or don't exist

A comment is worth the line it costs only when the code can't say it for itself: a
non-obvious invariant, a workaround for something outside this file, a subtle ordering
requirement. Never narrate what the next line already says.

**Earns its place** (from `scripts/check_licenses.py`):

```python
FORBIDDEN: dict[str, tuple[str, ...]] = {
    # Order matters: the specific families must be tested before the general one.
    # "GNU Affero General Public License" and "GNU Lesser General Public License"
    # both contain "general public license", so AGPL and LGPL come first. The SPDX
    # short identifiers "agpl" and "lgpl" (PEP 639 licence expressions, e.g.
    # "AGPL-3.0-only") likewise both contain "gpl", so they too must be tested
    # before the bare "gpl" needle. Do not add a bare "bsl" needle: BSL-1.0 is the
    # (permissive) Boost Software License, distinct from BUSL, the Business Source
    # License.
    "AGPL": ("affero", "agpl"),
    "LGPL": ("lesser general public", "lgpl"),
    "GPL": ("general public license", "gpl"),
    "BUSL": ("business source", "busl"),
    "SSPL": ("server side public", "sspl"),
}
```

Nothing in the dict literal itself tells you *why* the order matters; the comment carries
information the code cannot.

**Narration — delete on sight:**

```python
# increment the counter by one
counter += 1

# loop over the tables
for table in tables:
    ...
```

If ruff is silent about a style choice, the style is fine — style review is ruff's job, not
a reviewer's or an agent's. Don't send an LLM to do a linter's job.

## Pinned-tool policy

`ruff` (`0.16.7`) and `ty` (`0.0.80`) are pinned to exact versions in the root
`pyproject.toml` `[dependency-groups] dev` list — no `>=`, no floating range. `ty` is still
`0.0.x`; its behaviour can change between patch releases, so an upgrade is a deliberate PR
that bumps the pin and fixes whatever it flags differently, never a side effect of an
unrelated change. The same discipline applies to `ruff`: a version bump is its own commit
so a lint-rule change is never hidden inside a feature diff.

## Duplicated code

CI's `lint` job fails when a change adds copied code to `packages/*/src` (see "The duplication
ratchet" in `docs/agent/testing.md`). The message lists the new blocks as
`file:start-end ↔ file:start-end`. Handle a finding in this order:

1. **Extract** the shared code into one function (or class) in the lowest package both copies may
   import (`lakana-core` for the server and the worker), and call it from both places.
2. **Justify** a copy that has to stay (two packages that must not import each other, a block that
   mirrors a wire format): wrap it in `# jscpd:ignore-start` and `# jscpd:ignore-end`, with a
   comment beside them that says why. Ignored blocks are not counted; the reason is reviewed in
   the pull request.
3. **Never** raise `scripts/duplication-baseline.json` by hand. If duplication fell, lower it in
   the same pull request: `uv run python scripts/duplication_ratchet.py --update`.

## The `ui/` project

`ui/` is the web interface: a standalone npm project (React, TypeScript, Vite) whose build lands in
`packages/lakana-server/src/lakana_server/ui_dist/` (git-ignored) and ships inside the server
wheel. It has no version of its own (rule 3): `package.json` is `private` and carries none. Its
gates are in "The `ui/` gates" in `docs/agent/testing.md`.

- **Every CLI command and REST route has an entry in `ui/parity.yaml`.** The key is the command
  path (`table undrop`) or `METHOD /api/v1/path`; the value is `class: ui` or `read` with a
  `part` 1-8 of the interface, or `class: git` with a `reason`. The principle: whatever a person
  does by hand is in the UI; whatever a Git repository defines (assets, schedules, access files,
  deploys) or local tooling does (`init`, `profile`, `load`) is Git. `exception` is reserved for
  the two task-token minting entries. `scripts/tests/test_ui_parity.py` walks the live command
  tree and reads the routes from the committed `docs/site/reference/openapi.json` (kept equal to the app
  by CI's `generate_reference.py --check`) and fails on a missing, stale or invalid entry, naming it and the next step.
- **Exact pins, one tool per pull request.** Every dependency is an exact version
  (`ui/.npmrc`: `save-exact`, `engine-strict`, `ignore-scripts`), Node is the `26` line
  (`ui/.nvmrc`, `engines.node`). Dependabot groups what ships (`ui-runtime`) and the few
  development pins that only work together (`ui-vitest`, `ui-size-limit`, `ui-react-types`,
  `ui-testing-library`); every other tool upgrades alone, like ruff and ty above.
- **ESLint 9 and TypeScript 6.0, on purpose.** ESLint 10 exists, but `eslint-plugin-jsx-a11y`
  6.10.2 supports ESLint 9 at most, so Dependabot ignores major updates of `eslint` and
  `@eslint/js` (which moves with it, never ahead). TypeScript 7 is stable, but
  `typescript-eslint` 8.72.1 declares `typescript <6.1.0`, so Dependabot gives `typescript`
  patch updates only; `typescript` and `typescript-eslint` stay in separate Dependabot pull
  requests, and a newer TypeScript comes in its own pull request once typescript-eslint
  supports it.
- **One override, tracked for removal.** `ui/package.json` has
  `"overrides": {"minimatch@^10": "11.0.5"}`: advisory GHSA-g3hv-75wg-m249 (a denial of service
  in minimatch below 11.0.1) reaches the lock through `typescript-eslint`, whose
  `@typescript-eslint/typescript-estree` still asks for `minimatch ^10`. The use is dev-only and
  lint-time (it matches the repository's own globs, nothing in the build contains it), but
  `npm audit --audit-level=low` would fail, rightly. Remove the override when typescript-eslint
  allows minimatch 11 (issue #507); `scripts/tests/test_ui_project.py` pins it so that the removal
  is deliberate. No other override without an entry here.
- **TSDoc on every export and every props member.** ESLint's `jsdoc` rules (TypeScript mode,
  TSDoc tags accepted) fail an undocumented exported function, class, interface, type, enum or
  interface member. Inline comments follow "Inline comments" above.
- **Only `ui/src/api/` talks to the server** (rule 2: the REST API is the only way in). ESLint
  refuses `fetch`, `XMLHttpRequest`, `WebSocket`, `EventSource`, `window.fetch`,
  `globalThis.fetch` and `navigator.sendBeacon` everywhere else, and HTTP client packages
  (`axios`, `ky`, `ofetch`, `superagent`) everywhere. dependency-cruiser keeps `src/api/` a leaf:
  it imports only itself and npm packages (its own tests may also use the helpers in
  `src/test/`). (`fetch` is a global, not an import, so
  dependency-cruiser cannot see it; ESLint guards it.)
- **No Node globals in browser code.** `globals.node` is on for the config files and scripts, so
  ESLint refuses `process` and `Buffer` inside `ui/src/` (`src/api/` included): Vite does not
  polyfill them, so they would fail only in the browser. Use `import.meta.env` and `Uint8Array`.
- **The content security policy stays strict** (`ui/csp.ts`: no inline script, no inline
  style but the exact texts it hashes). The stored theme is applied before the first paint by a
  file, `ui/public/theme-boot.js`, not an inline script. React Aria inserts two `<style>`
  elements at run time (`usePress`: `touch-action` on pressable elements; `usePreventScroll`:
  `overscroll-behavior` while a modal is open on iOS and iPadOS); `style-src` lists the
  `sha256` hash of each exact text (`LIBRARY_STYLE_HASHES`), never `'unsafe-inline'`.
  `ui/build/csp.test.ts` counts the places React Aria, React Aria Components, React Stately and
  React Router insert a `<style>` and hashes their texts, so an upgrade that changes or adds one
  fails there; `e2e/shell.spec.ts` "no console error" opens the menu and the palette too.
- **Every detail page has a breadcrumb** (a person, later a table, a job, a run, a task): pass
  `trail` to `Page` (`ui/src/shell/Page.tsx`), the parts above the page from its area down, e.g.
  Access › People › test1 for `/access/people/test1`. `Page` renders the one shared component,
  `ui/src/shell/Breadcrumbs.tsx`, above the heading: a `nav` landmark named "Breadcrumb", every
  part but the last a link, `aria-current="page"` on the last, separators hidden from screen
  readers, wrapping at 320 px. An area's own pages (a list, a tab of the area) have none.
- **Every screen pull request is checked against the project skill `lakana-ui`**
  (`.claude/skills/lakana-ui/SKILL.md`): the Still Water tokens and component rules, one pattern
  per area, rows that open their object, breadcrumbs, refusals that name the cause and the next
  step, the accessibility rules and the forbidden list (what makes an interface look generated).
  The reasons are on the contributor page `docs/site/explanation/how-lakana-looks.md`; change
  the skill and the page together.
- **Every string the user sees goes through i18next** (`ui/src/i18n/en.json`), from the first
  stylesheet and string on (part 1a, Task 3).
- **Licences.** The npm locks are checked like the Python one. The MPL-2.0 development tools
  (`axe-core`, `@axe-core/playwright`, `lightningcss` and its platform packages) and the OFL-1.1
  fonts are the only exceptions; see "Copyleft rule: none strong, weak only via a justified
  allowlist entry" below and its "Fonts: SIL OFL 1.1".
- **Licence notices ship with the bundle.** The minifier strips the bundled packages' licence
  headers, so `vite.config.ts` sets `build.license` and the build writes every bundled package's
  name, version and licence text to `ui_dist/licenses/THIRD-PARTY-NOTICES.md` (served at
  `/licenses/THIRD-PARTY-NOTICES.md`); `e2e/notices.spec.ts` fails when a runtime dependency of
  `package.json` is missing from it, and the release's wheel check fails without the file.

## Upper bounds on the deeply used dependencies

`sqlglot` and `pyiceberg` (`lakana-core`) and `duckdb` (`lakana-worker`) carry an upper bound:
`sqlglot>=30.21.0,<30.25`, `pyiceberg>=0.12,<0.13`, `duckdb>=2.0,<3`. Lakana uses their APIs deeply
(`sqlglot` drives lineage and the query rewriter for row filters and column masks), and
they change signatures in minor releases. The lock does not protect a user: `pip install` ignores
it, so an unbounded requirement takes the newest release on the day it is installed (a `sqlglot`
release once made an argument of `sqlglot.lineage.lineage` required, and only an unpinned CI run
noticed). `scripts/tests/test_dependency_bounds.py` fails when one of the three has no upper bound.
`duckdb` also has a lower bound that matters: the query process configures DuckDB with settings
only 2.0 has (`extension_directories`, `allow_extension_repositories`, `async_threads`), and the
same test pins `duckdb>=2.0,<3`. Moving to DuckDB 3 means sweeping the guard's function lists
(`query_rewrite.py`) and the query process's settings (`query_child.py`) again, as #96 did for 2.0.

**`sqlglot` is held below 30.25** (issue #415): from 30.25 its qualifier refuses an unqualified
column in a subquery, or a `SELECT` alias in `WHERE`, when no schema is given ("Cannot resolve
'a' due to missing source schemas"), so the guard would refuse ordinary queries and check
queries, and its DuckDB parser no longer records that a lambda was written with `LAMBDA`. A hold
below a minor release is also an `ignore` in `.github/dependabot.yml`, or the weekly group raises
the bound itself (#528 did); the same test fails when the two disagree. Lift both together, once
the guard is adapted without weakening a check.

To raise a bound: its own pull request (the Dependabot updates keep the lock current inside the
bounds), after reading the upstream changelog, with the suite green on the new version.
The daily dependency canary (`canary.yml`, `docs/agent/testing.md`) covers a break inside the bounds.
Other dependencies stay open-ended on purpose; add one to the test when Lakana starts to depend on its
internals the same way.

## Copyleft rule: none strong, weak only via a justified allowlist entry

No strong copyleft (GPL, LGPL, AGPL) or source-available (BUSL, SSPL) licensed dependency,
ever — no allowlist exists for these, and none should be added. Weak/file-level copyleft
(MPL, EPL, CDDL, EUPL) is permitted only when the package is used unmodified and only via
a new, justified entry in `scripts/check_licenses.py`'s `ALLOWLIST`; unlisted, it fails the
build the same as strong copyleft. `scripts/check_licenses.py` runs in `security.yml` on
every PR, over an environment with every dependency group **and every extra of every
workspace package** (`uv run --all-groups --all-extras --all-packages pip-licenses`): a
dependency only `lakana[assets]` pulls in is judged like any other.
It matches license metadata case-insensitively against ten copyleft families (short ids match
as whole tokens, so `Simplified BSD` is not MPL), with
each family's more specific needle checked before a more general one that would also match
it (AGPL/LGPL before the bare GPL needle, since their names contain it).

The same script covers **npm**: any `package-lock.json` in the repo (lockfileVersion 2/3,
which records a `license` per package) is checked under the same families and the same
`ALLOWLIST` (matched by package name). SPDX expressions are parsed: `OR` passes if any
alternative is permissive, `AND` fails if any term is forbidden. A package with no `license`
field fails as UNKNOWN, a malformed expression (unbalanced parenthesis, dangling `OR`) as
UNPARSEABLE, and `UNLICENSED`, `SEE LICENSE IN ...` and `LicenseRef-*` as NO-GRANT/OPAQUE unless
allowlisted. Workspace members and `link` entries are first-party and skipped. `pnpm-lock.yaml`, `yarn.lock` and v1 lockfiles are refused with an
"unsupported lockfile" error rather than skipped. No lockfile means the npm part is a no-op.
E.g. Shepherd.js and Intro.js (AGPL) are refused; Driver.js and react-joyride (MIT) pass.

`ALLOWLIST` holds these entries today, each with its own justifying comment: `hypothesis`
(MPL-2.0, `dev` dependency-group only, never shipped), `pathspec` (MPL-2.0, transitive dep of
`hatchling`, `dev` only) and `certifi` (MPL-2.0, a direct dependency of `lakana-worker`,
whose query process falls back to its CA bundle, and a transitive dependency of `requests`
that `pyiceberg` requires — present in the default, shipped dependency set, allowlisted because MPL-2.0's share-alike obligation attaches only to
modifications of certifi's own files, never to code that merely imports it, and certifi is
used unmodified).
The npm side adds `axe-core` and `@axe-core/playwright` (MPL-2.0, test-time only, never in the
bundle) and `lightningcss` with its platform packages (MPL-2.0, Vite's CSS transformer, a build
tool used unmodified, never shipped).

### Fonts: SIL OFL 1.1

The SIL Open Font License is a tenth family in the check (`OFL`). It is accepted only for the
bundled, unmodified font files of the packages in `FONT_PACKAGES`
(`@ibm/plex-sans`, `@ibm/plex-mono`) and only on the npm side; the same licence on any other
package, or Python metadata, fails. Never subset a font with a Reserved Font Name: IBM Plex
reserves "Plex", so only the `fonts/complete/woff2/` files are used. The OFL text ships beside
the fonts.

The design decision (architecture Constraint 8) is pg8000 (BSD-3) as the default Postgres
driver, with psycopg2 (LGPL — forbidden outright, no allowlist possible under the rule
above) available behind a `lakana-server[psycopg]` extra a caller must opt into — chosen so
the default dependency set needs no copyleft exception for its database driver.
**Confirmed current state, not the M1-era placeholder this paragraph used to describe:**
`packages/lakana-core/pyproject.toml`'s `dependencies` list is no longer empty — it now
carries `sqlalchemy`, `alembic`, `pydantic`, `pydantic-settings`, `pyiceberg`, `fsspec`,
`pyarrow`, `s3fs`, `sqlglot`, `cryptography`, and, as of M4d's Compose team bundle
(`postgresql+pg8000://` in `compose.yaml`'s connection string), `pg8000>=1.31` itself —
`pg8000` is now a **default runtime dependency of `lakana-core`**, not a dev-only one; it also
still appears in the root `pyproject.toml`'s `dev` dependency group, for the
`testcontainers`-backed integration tests, which is redundant with `lakana-core`'s own copy
but harmless. `packages/lakana-server/pyproject.toml` declares `fastapi`, `pyarrow`, and
`uvicorn` under `[tool.hatch.metadata.hooks.custom]` (its `hatch_build.py` hook appends a
`lakana-core==<version>` pin to that list) and no `[project.optional-dependencies]`
section — the `lakana-server[psycopg]` extra above is still design intent, not built yet.
Don't try `uv sync --extra psycopg` expecting it to exist.

### Native code inside a wheel

pip-licenses reads Python metadata only. A wheel can carry compiled libraries that metadata
never names: auditwheel's `<package>.libs/` directory, a statically linked library, or code a
package downloads when it runs. Before a dependency with compiled code lands:

1. Install it into a scratch directory (`uv pip install --target ~/.cache/licence-check/<name> <name>==<version>`).
2. List what is bundled: `find ~/.cache/licence-check/<name> -name '*.so*' -o -path '*.libs/*'`,
   `ldd` on each shared object, `strings <object> | grep -iE 'openssl|libpq|gpl|lgpl|copyright'`,
   and the licence files in the wheel's `.dist-info` (and any `LICENSE*` beside the objects).
3. Judge each bundled library's licence by the rule above. A copyleft one rejects the
   dependency (connectorx, which bundles libkeyutils under LGPL-2.1, is the example).
4. Record the result in the pull request. An `ALLOWLIST` entry for such a dependency names the
   bundled library and its licence in its comment.

Pillow 13.0.0 (MIT-CMU; `lakana-core`, profile pictures) is the worked example. Its manylinux
wheel's `pillow.libs/` holds libjpeg-turbo (IJG, BSD-3-Clause), libpng (libpng-2.0), libwebp
with sharpyuv, demux and mux (BSD-3-Clause), libtiff (libtiff), FreeType (FTL, the option it offers beside
GPL-2.0), HarfBuzz (MIT), Little CMS (MIT), OpenJPEG (BSD-2-Clause), libavif (BSD-2-Clause, with
dav1d and aom linked in, both BSD-2-Clause), brotli (MIT), xz (0BSD), zstd (BSD-3-Clause), libxcb
and libXau (X11); zlib comes from the system (manylinux) or as zlib-ng (Zlib) on macOS. The
wheel's SBOM (`.dist-info/sboms/`) also names FriBiDi (LGPL-2.1) and libimagequant (GPL-3.0):
neither is in the wheel. Pillow loads FriBiDi only from the system, for text layout Lakana
never uses (the server image has none), and its wheels are built without libimagequant
(`PIL.features.check("libimagequant")` is false). All permissive: no `ALLOWLIST` entry.

## Paginated list endpoints

A list endpoint that can grow with ordinary traffic returns `{"items": [...],
"next_cursor": str | null}` and accepts `?limit=` and `?cursor=`. The cursor is an opaque,
unpadded base64url keyset position over the endpoint's own sort key, with a unique
tiebreaker (`id`), never an offset. Filters are not encoded in it; the client resends
them with every page. `GET /api/v1/audit-events` is the reference implementation, and the
SDK exposes such an endpoint as `list(..., cursor=)` (one page) plus `iter()` (every page).

An endpoint that already returned a bare JSON array before it was paged keeps that body, so
its clients do not break, and carries the cursor in the `X-Next-Cursor` response header
(absent on the last page) instead: the runs, queries, deploys, grants, principals and groups
lists. Its cursor carries the list kind, so a cursor from another list is refused.

## Two ruff quirks worth knowing

Both come from `preview = true` plus `select = [..., "RUF", ...]` in `pyproject.toml`.

1. **Rule *codes* are rejected in ignore/select selectors — use rule names.** This is
   `RUF201` (`rule-codes-in-selectors`): with preview rules on, a config that writes
   `ignore = ["D105"]` fails; it must read
   `ignore = ["undocumented-magic-method"]`. The repo's own `per-file-ignores` and
   top-level `ignore` list already follow this (see `pyproject.toml`) — don't reintroduce a
   bare code when adding a new ignore.
2. **Float equality trips `RUF069`** (`float-equality-comparison`, preview-only). `a == b`
   or `a != b` on floats is flagged; use `math.isclose()` (or an explicit tolerance) instead.
   This applies even in test assertions — compare coverage percentages, timings, or other
   floats with `math.isclose`, not `==`.

## Ease of use and user documentation

Lakana is for small and medium teams, many without senior data engineers. The reader to
design for knows SQL and git and some Python, and is new to Lakana and often to running a data
platform; the scope page's "Who it is for" says who that is. Two things follow, and both are part
of "done".

**Design for a first-time user.**
- **One obvious way.** Do not add a second way to do something that already has one. Do not add
  an option nobody has asked for.
- **Defaults that work.** A new setting, flag or front-matter key has a default that is right for
  a small team on one machine. Requiring a value is the exception and needs a reason.
- **Errors say what to do next.** A message names what was wrong, where (file and line, the
  setting's name, the table), and the next step. "Invalid value" is not a message. Never make a
  user read a traceback to learn what to fix.
- **Fail early.** What can be checked at deploy or at startup is checked there, not at the first
  run in production.
- **Names are plain words.** No internal jargon in anything a user reads: CLI output, error
  messages, settings, documentation.
- **Safe by default.** The easy path is the safe one; anything destructive or affecting a
  protected environment asks for an explicit step.

**User documentation ships with the change.**
- User documentation lives in `docs/site/` (one directory per kind, below); `mkdocs.yml` lists
  the pages. `docs/agent/*` is not part of it. How to add a page, a code block or an example:
  "The documentation site" in `docs/agent/testing.md`.
- A change that adds or changes user-facing behaviour (CLI, SDK, REST, settings, front-matter,
  errors a user will meet) updates the user documentation in the same change. Rule 6 covers
  statements the change makes false; this covers what the change makes new.
- Write for the reader who has not seen the feature: say what it is for before how it works, and
  show a complete example that can be copied and run.
- Documentation comes in four kinds; put new text in the right one:
  - **Tutorial:** learn by doing, start to finish.
  - **How-to guide:** one task, the shortest path.
  - **Reference:** exact facts — every option, default and limit.
  - **Explanation:** why it works this way.
- Code a page shows comes from a tested example (`docs/examples/`, run by CI) or is checked by
  `scripts/docs_blocks.py`; a block marked "not tested" needs a reason and is counted. A snippet
  nothing exercises is a defect.
- Agent-facing notes (`docs/agent/*`) are not user documentation. Updating them does not satisfy
  this rule.

Reviewers check both parts: a user-facing change without a default that works, a clear error
or its documentation is not ready to merge.

## Log lines worth an alert

A log line an operator may want an alert for (a stop, a lost worker or claim, a database or
storage that does not answer, a failing sweep) carries a stable code:
`logger.warning("...", extra=log_event(LogEvent.X))` from `lakana_core.log_events`; the formatter
ends the message with ` event=<code>`. A new code needs its member in `LogEvent`, its process in
`WHERE`, its row in `docs/site/reference/log-events.md` (same order), and a test of its call site
that asserts the code; `scripts/tests/test_log_events_doc.py` fails otherwise. A code is never
renamed or reused; keep the wording of a line free to change, the code is the contract.

## Names

A namespace component or table name must be non-empty, not whitespace-only, and must not
contain `.`, `/`, a NUL byte, or the unit separator (`validate_path_component`). A *new* table or
namespace name must not contain `%` either (`refuse_percent_in_new_name`: a storage may decode `%41`
to `A`, so two names would share a directory). That check is called only where a table or namespace
name is created (`create_namespace` for each new component, `ResolvedStorage.table_location`,
`create_external_table`, new views, the `rename_table` destination and the `undrop_table` new
name), never in parsing, discovery, matching or a lookup, and never for job, check, repository,
package-index or artifact names. The `.`
rule keeps the dotted display form (`gold.sales`) unambiguous: `["a.b"]` and
`["a", "b"]` would otherwise render identically.

Names created before those rules may contain a dot or a `%`. They stay readable, listable, droppable
and renamable *away*, and are **deprecated**: only creating a name, or renaming *to* one,
is refused. So `NamespacePath.parse` (stored and URL-encoded names) and
`from_parts(..., allow_dots=True)` (Iceberg REST create/rename bodies, which may sit under
an existing dotted parent) accept dots, and `CatalogService` validates the *new* component
itself (`create_namespace` checks every component whose ancestor prefix is not already an
existing namespace row, since missing parents are created implicitly; a dotted component is
exempt only when the namespace up to and including it exists — a child under a legacy
parent; `create_table`, `create_external_table`, new views,
`rename_table` destination). Never call `validate_path_component` without `allow_dots` on a
lookup path.

Every `uses:` in `.github/workflows` names a full commit hash with the version in a trailing
comment (`uses: actions/checkout@<40 hex>  # v7`); a tag can be moved after the fact, a hash
cannot. Dependabot's `github-actions` updates rewrite both. `scripts/tests/test_workflows.py`
fails on a tag or branch reference. To pin a new action, resolve the tag with
`gh api repos/OWNER/REPO/git/ref/tags/TAG` and, when `object.type` is `tag`, follow it through
`git/tags/<sha>` to the commit.
