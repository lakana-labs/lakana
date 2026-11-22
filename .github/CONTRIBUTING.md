# Contributing to Lakana

## How to contribute

Lakana is open source, but its code comes from its maintainers. Everyone is welcome to:

- **Report a bug or propose a change** by [opening an issue](https://github.com/lakana-labs/lakana/issues/new/choose).
  A clear issue with steps to reproduce helps the project most.
- **Report a security vulnerability** privately, as [SECURITY.md](SECURITY.md) describes.

Pull requests are by invitation. When a maintainer would welcome a pull request for an issue, they
add the label `pr-welcome` to it; open your pull request with `Closes #<that issue>` in its
description. A pull request from outside the maintainers without such an issue is closed
automatically, with a pointer back to the issue tracker. This is not a judgement of your work: it
keeps the project's design and review load in a few hands.

You are of course free to fork Lakana under its license.

## Before your first pull request

You must sign the [Contributor License Agreement](https://github.com/lakana-labs/lakana/blob/main/docs/legal/CLA.md). A bot will comment
on your first PR with what to write; signing takes one comment.

In plain words: you keep the copyright in your contribution. You license it to the
maintainer and to whoever maintains the project after them, including the right to license
the project, with your contribution in it, under other terms later. A version we have
published under Apache-2.0 keeps that license. If you contribute for an employer, your
employer also signs the [corporate agreement](https://github.com/lakana-labs/lakana/blob/main/docs/legal/CLA-CORPORATE.md), and you still
sign the individual one. The agreement says how your signature is stored and for how long.

## Workflow

Lakana uses trunk-based development. `main` is always releasable.

1. Branch from `main` using [Conventional Branch](https://conventional-branch.github.io/)
   naming, `<type>/<lowercase-description>` with a commit type as the prefix (`feat/`,
   `fix/`, `perf/`, `revert/`, `docs/`, `refactor/`, `test/`, `ci/`, `build/`, `chore/`).
   CI rejects any other branch name.
2. Write the test first. Lakana is test-driven; a PR that adds behaviour without a test that
   failed before it will be sent back.
3. Open a PR. The title must follow
   [Conventional Commits](https://www.conventionalcommits.org/) —
   `<type>(<scope>)!: <summary>`, linted by commitlint (`commitlint.config.mjs`, extending
   conventional-changelog's Conventional Commits preset). PRs are squash-merged, so the
   title becomes the commit subject and the description its body; together they drive
   the version bump and `CHANGELOG.md`. The PR template's footer paragraph must stay
   last: one `Closes #N` per issue, and a `BREAKING CHANGE: <migration note>` line when
   the title carries `!`.
4. Every PR is reviewed before merge.

   While you are still iterating, open the PR as a draft: CI then skips its slowest jobs
   (integration, the five-minute test, the Compose smoke test and Python 3.11). Marking it
   ready for review starts the full run. The checks that gate the merge (`lint`, `test (3.11)`,
   `test (3.14)`, `conventional`, `static`, `secrets`, `integration` and `docs-examples`) must
   be green. `five-minute` and `compose-smoke` are not required, but if one is red or skipped,
   `main` runs everything again after the merge.

## Running the gates locally

```bash
uv sync --all-packages
uv run ruff check . && uv run ruff format --check .
uv run ty check packages
uv run lint-imports
uv run pytest --cov --cov-report=json
uv run python scripts/coverage_ratchet.py
```

The ratchet above runs at its real 90% floor, identically to CI — see
`docs/agent/testing.md` for how it works and how to raise the baseline.

A pull request that changes only documentation skips CI's heavy jobs and runs the
documentation tests (`uv run pytest scripts/tests`); the same file explains the rule.

Documentation pages live in `docs/site/`. Check, build and preview them with
`uv run python scripts/docs_blocks.py`, `uv run python scripts/generate_reference.py --check`
(rewrites the generated CLI, settings and OpenAPI references without `--check`), `uv run --group docs zensical build --strict`
(then `uv run python scripts/check_built_site.py site-build`) and
`uv run --group docs zensical serve`
(`docs/agent/testing.md`, "The documentation site", explains the code-block markers).

## Code conventions

- **Numpydoc docstrings on every module, class, function, and method.** Concise but
  complete: what it does, its parameters, what it returns, what it raises. ruff requires
  the docstring to exist and checks it against the signature for a missing `Returns`, an
  undocumented `Raises`, and a documented parameter that no longer exists — it does **not**
  catch a parameter left undocumented, so that gap is a review responsibility, not a lint
  one. See `docs/agent/conventions.md`.
- **Inline comments only where the code genuinely needs explanation** — a non-obvious
  invariant, a workaround, a subtle ordering requirement. Never narration of the next line.
- Style is ruff's job, not a reviewer's. If ruff is silent, style is fine.

## Package boundaries

`lakana-server` and `lakana-worker` both depend on `lakana-core`, and never on each other — they
communicate only through `query_runs` rows written and read via `lakana-core`. `lakana` depends
on none of them. `lakana-core` must never import a web framework; `lakana-server` must never
touch storage or Iceberg directly; `lakana-worker` is the only package allowed to import
`duckdb`. import-linter enforces this.
