# Lakana

A lightweight open source data lakehouse platform for small and medium teams. DuckDB for
compute, Apache Iceberg for tables, Postgres for catalog metadata. Production code lives in
git repositories as Python and SQL files; the platform does not host notebooks. Current scope
and plan: `docs/site/explanation/scope.md` and `docs/site/explanation/roadmap.md`.

## Packages

| Path | Import | Responsibility |
|---|---|---|
| `packages/lakana-core` | `lakana_core` | Domain and persistence. Storage, Iceberg, catalog. **Never imports a web framework.** |
| `packages/lakana-server` | `lakana_server` | FastAPI control plane and Iceberg REST catalog. **Never imports pyiceberg or fsspec** — it calls core. |
| `packages/lakana-worker` | `lakana_worker` | The DuckDB query executor and dequeue loop. **The only package allowed to import `duckdb`.** Never imports `lakana-server`. |
| `packages/lakana` | `lakana` | SDK and CLI. Depends on none of the above. |

import-linter enforces these boundaries; `uv run lint-imports` checks them.

## Non-negotiable rules

1. **Write the test first.** A change without a test that failed before it does not land.
2. **The REST API is the only way in.** No component gets a privileged side channel — not
   the CLI, not the UI, not user code we execute ourselves.
3. **Never hand-edit a version.** All four packages derive one version from git tags.
4. **No strong copyleft dependency (GPL, LGPL, AGPL, SSPL, BUSL), ever.** Weak/file-level
   copyleft (MPL, EPL, CDDL, EUPL) only unmodified and only via a justified entry in
   `scripts/check_licenses.py`'s `ALLOWLIST` — CI fails otherwise. Covers Python **and npm**:
   any `package-lock.json` is checked (SPDX `OR` passes if any alternative is permissive;
   a missing licence fails); pnpm/yarn lockfiles are refused until supported.
5. **Run the gates before claiming done.** Locally, before a push: `ruff check`,
   `ruff format --check`, `ty check packages`, `lint-imports`, and the tests of the changed
   packages and their dependents (`scripts/select_tests.py` prints them; `lakana-core`,
   shared fixtures, `pyproject.toml`, `uv.lock` or `scripts/` select everything). In CI only:
   `pytest --cov --cov-report=json` with `scripts/coverage_ratchet.py`, and Python 3.11, 3.13
   and macOS. A local pytest is not a ratchet result and must never be presented as one — the
   ratchet refuses a report older than the code rather than lie
   (`docs/agent/testing.md`, "Local gates").
6. **When you change a rule, a config, or a licence, grep the docs for what asserted the
   old behaviour and fix it in the same commit.**
7. **Conventional everything** — CI's `conventional` check enforces all of it:
   - **Branch:** `<type>/<lowercase-description>`, e.g. `feat/m4b-cli`
     ([Conventional Branch](https://conventional-branch.github.io/)).
   - **PR title** (the squash commit's subject): `<type>(<scope>)!: <summary>`
     ([Conventional Commits](https://www.conventionalcommits.org/), linted by commitlint
     against `commitlint.config.mjs`). Types: `feat`, `fix`, `perf`, `revert`, `docs`,
     `deps` (Dependabot) — these appear in the changelog — and `refactor`, `test`, `ci`,
     `build`, `chore`, which don't. Optional scope from: `core`, `server`, `worker`, `sdk`,
     `cli`, `catalog`, `query`, `policy`, `secrets`, `audit`, `security`, `release`, `ci`,
     `docs`, `deps`.
   - **PR description** (the squash commit's body) ends with a footer paragraph: one
     `Closes #N` **per issue**, and — when the title has `!` — a
     `BREAKING CHANGE: <what changed and how to migrate>` line, which release-please
     copies into `CHANGELOG.md`. Details: `docs/agent/release.md`.
8. **Ease of use and user documentation are part of every change.** Many of the teams Lakana
   is for have no senior data engineers. Anything a user sees ships with a default that works,
   an error message that names the cause and the next step, and its user documentation in the
   same change. Details: `docs/agent/conventions.md`, "Ease of use and user documentation".

## Talking to the maintainer

Terse. Bullets over prose, short status lines, no preamble and no recap of what was just
said. Flag findings and warnings with an emoji and then **compress** what follows — the
emoji earns its place by making the important line findable, not by decorating a paragraph.

Keep every fact that changes a decision: the number, the file, the failing case, the
trade-off. Brevity means cutting words, never cutting substance — a finding stated in one
line still needs its evidence. Answer questions with the context needed to judge the
answer, and stop there.

## Choosing a model for a subagent

Always pass one explicitly. Omitting it inherits the caller's model, which is usually the
largest one, and that silently defeats this section.

| Work | Model |
|---|---|
| Transcription: the task text already contains the code to write | smallest |
| Single-file mechanical edits, doc-only changes | smallest |
| Implementation from a prose description; scoped re-reviews of small fix diffs | mid |
| Multi-file work needing integration judgement; full task reviews | mid, or high when the diff is large or security-relevant |
| Architecture, design, and the final whole-branch review | largest |

The smallest models often take two or three times the turns on
multi-step work, so treat mid as the floor for anything that has to
reason about a codebase rather than copy from a spec.

Security-relevant diffs earn a tier. Every path-traversal escape found in this repo so far was
found by a reviewer reasoning adversarially about code that already looked correct.

## Design documents

- `docs/site/explanation/scope.md` — current scope, principles and audiences (living)
- `docs/site/explanation/roadmap.md` — the milestone sequence to v1.0.0 (living)
- `docs/site/` — the user documentation site; adding a page, a code block or an example:
  "The documentation site" in `docs/agent/testing.md`
- `docs/agent/architecture.md` — component boundaries and system-wide constraints
- `docs/security/threat-model.md` — the living threat model

<important if="you are writing Python">
Read `docs/agent/conventions.md`. Numpydoc docstrings are required on every module, class,
function, and method, every parameter documented. ruff catches a missing `Returns`, an
undocumented `Raises`, a documented parameter that no longer exists, and — only once a
`Parameters` section exists — one left out of it; omitting that section entirely slips
through, so that's on you, not the gate. Inline comments only where the code genuinely
needs explanation.
</important>

<important if="you are adding or changing anything a user sees (CLI, SDK, REST, settings, errors, docs)">
Read "Ease of use and user documentation" in `docs/agent/conventions.md`. Design for someone
meeting the feature for the first time: one obvious way, a default that works, an error message
that says what to do next, and the user documentation in the same change. A feature without its
user documentation is not finished.
</important>

<important if="you are writing or changing tests">
Read `docs/agent/testing.md` for the unit/integration split, fixtures, and the coverage
ratchet. Integration tests use testcontainers against real Postgres and SeaweedFS (S3), never mocks.
</important>

<important if="you are touching authorization, policy, secrets, or the query path">
Read `docs/agent/security.md` first. Authorization call sites exist from M1 as allow-all —
M3 replaces the implementation, never inserts the call sites. Query isolation works by
credential scoping, not by blacklisting DuckDB functions.
</important>

<important if="you are looking for where something is defined or what calls it">
`docs/agent/graphify.md`. `graphify explain <symbol>` is a cheap first hop for
structural questions. It is a lead generator, never an authority: it cannot show
that something is **absent**, so completeness checks still belong to `grep`.
</important>

<important if="you are adding or upgrading a dependency">
No strong copyleft (GPL, LGPL, AGPL, SSPL, BUSL) — ever, no allowlist. Weak/file-level
copyleft (MPL, EPL, CDDL, EUPL) is permitted only unmodified and only with a new,
justified entry in `scripts/check_licenses.py`'s `ALLOWLIST`; unlisted, it fails CI the
same as strong copyleft. `ty` and `ruff` are pinned exactly and upgrade only in their own
PR. See `docs/agent/conventions.md`.
</important>

<important if="you are cutting a release">
Read `docs/agent/release.md`. Releases happen by merging the release-please PR; never tag
by hand.
</important>
