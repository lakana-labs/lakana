# Graphify

[Graphify](https://github.com/Graphify-Labs/graphify) builds a queryable graph of
the codebase — symbols, their definitions, and the edges between them. We use it
to **navigate**, not to decide.

## What it is for

Answering structural questions without reading whole files:

```
graphify explain "CatalogService"     # what it is, what it touches
graphify path A B                     # how two symbols connect
```

`explain "CatalogService"` takes a fraction of a second and makes no model calls; it
summarises what reading several files would show.

## What it is not for

**Never use it to establish that something is absent.** A graph query returning
nothing means the query found nothing; it does not mean the thing does not
exist. Completeness checks — "is this the only call site", "does any workflow
still reference the old flag" — belong to `grep`, which searches text and cannot
quietly omit a node.

**It does not detect documentation drift.** This is worth stating plainly
because it was the initial argument for adopting the tool and it turned out to
be wrong. `pyproject.toml` produces zero nodes and is absent from the graph
entirely, so a claim in a document about a setting in the config cannot be
traversed — half the relationship is not representable. Graphify's own warning
directs reports to
[#1666](https://github.com/Graphify-Labs/graphify/issues/1666), though that
issue is filed for Ruby and is closed, so treat it as the symptom class rather
than a tracked fix. Every drift defect we have found so
far came from a model reading two files in one context window. Keep doing that.

In review, a graph query is a lead generator. Findings still need the file read
and the failing case demonstrated.

## Staleness

A stale graph is worse than no graph, because it answers confidently. Ours was
stale within an hour of being built. The graph also describes the **working
tree**, not the repository: checking out a branch that lacks a package's commits
drops those nodes, which is correct behaviour and a good reason to let the hook
rebuild rather than to trust a graph built on another branch.

`graphify hook install` writes `post-commit` and `post-checkout` hooks into
`.git/hooks/`, which is not tracked, so this is per-clone and optional — but
run it. The AST refresh is `graphify update .`: about 40 s, no model calls.
Semantic re-extraction is the slow pass; run that at milestone
close, not per commit.

`graphify-out/` is gitignored. It is a local cache, not an artifact.

## Worktrees

The graph lives in the main clone's `graphify-out/`. A linked worktree has none, so pass
`--graph <main clone>/graphify-out/graph.json`. That graph describes main, not the branch's own
changes. Rebuild it with `graphify update .` in the main clone after merges and releases; it
takes about 40 s and makes no model calls.

## Do not commit `.gitattributes` for it

`graphify hook install` may write a `.gitattributes` registering a merge driver
for `graphify-out/graph.json`. Delete it. The file is gitignored, so the driver
can never fire, and the driver path points into a local uv tool directory that
does not exist in anyone else's clone.
