# The REST API as a contract

Everything in Lakana goes through its REST API: the CLI, the SDK, the code a deploy runs and
the engines that read tables. This page says which parts of that API you can build on, what
Lakana promises about them, what counts as a breaking change, and how a removal is announced
before it happens.

## Two surfaces

| Surface | Routes | Versioned by | Its document |
|---|---|---|---|
| The platform API | everything under `/api/` (today `/api/v1/`) | Lakana, by the rules on this page | [`openapi.json`](openapi.json) |
| The Iceberg REST catalog | everything under `/v1/` | the Apache Iceberg REST catalog specification | the Iceberg project's |

### The platform API

Environments, tables, queries, deploys, runs, jobs, grants, secrets, artifacts and the rest of
what the CLI and the SDK do. It is Lakana's own, so Lakana carries its compatibility promise
(below).

### The Iceberg REST catalog

The routes that other engines and Iceberg clients use to find and commit tables. Lakana implements version 1 of the Iceberg REST catalog API (the `/v1/` routes of the
Apache Iceberg REST catalog OpenAPI specification): the configuration
(`GET /v1/config`), namespaces (create, list, load, update properties, drop) and tables (create,
list, load, commit, rename, drop). It does not send an `endpoints` list, so a client uses the
specification's default set of endpoints. The specification, not Lakana, decides what these
routes look like, so they are not in Lakana's `openapi.json` and the promise below does not
cover their shapes. What Lakana does change there is which commits it accepts (its checks of a
commit); such a change is announced in the changelog and the [upgrade
notes](../explanation/upgrades.md) like any other.

A commit's snapshot summary may not carry `lakana.task-run-id` or `lakana.task-claim`: Lakana
removes both from every snapshot a client commits, because only the platform records which Python
task attempt wrote a snapshot.

## The promise

- **Before v1.0.0**, a minor release (0.N.0) may break the platform API, but only openly: the
  change is marked breaking, its `BREAKING CHANGE:` entry in the changelog says what changed and
  how to migrate, and the release's [upgrade notes](../explanation/upgrades.md) say what to do.
  A patch release (0.N.P) does not break it.
- **From v1.0.0**, a minor or a patch release never breaks the platform API. A breaking change
  waits for the next major release, and every route, field or parameter it removes is deprecated
  first (below).
- **An additive change can come in any release.** Build your client so that it keeps working
  when the API grows (below).

## What counts as breaking

A change is breaking when a call that worked, or a reading of an answer that worked, may stop
working:

- a route, or one method of a route, is removed;
- a parameter, a request field, a request body, a response field, a documented response or a
  media type is removed;
- a new parameter, request field or request body is required, or an optional one becomes
  required;
- a request value accepts less than before: a narrower type, a new or tighter limit (length,
  size, number of items) or pattern, a new list of allowed values, a changed default, or other
  fields refused;
- a response value may now be missing, `null`, or of another type;
- an allowed value of a field or a parameter is removed;
- the status code of a documented response changes (the old code is a removed response).

## What counts as additive

Everything else that changes the platform API:

- a new route, method, optional parameter or optional request field;
- a new response field, a new documented response, a new media type;
- a new allowed value of a field or a parameter;
- a request value that accepts more than before;
- a deprecation.

A change of wording (summaries, descriptions, titles) is neither: it is not part of the contract.

## What your client should allow for

The additive changes above arrive in minor releases, so a client that keeps working across
releases:

- ignores response fields it does not know;
- accepts a value of a field it does not know (an allowed value added since it was written), for
  example by treating it as "other";
- treats an error status it does not know by its class (`4xx`: the request, `5xx`: the server);
- sends only the parameters and fields it needs, and relies on the documented defaults for the
  rest.

## Deprecations

From v1.0.0, a route, a field or a parameter that is to be removed is deprecated first:

- **It keeps working** for at least two minor releases and at least three months, whichever is
  longer, and it is removed only in a major release.
- **It is marked** with `deprecated: true` in the OpenAPI document, on the route, the field or the
  parameter, and on its page in this documentation.
- **A deprecated route says so in its answers**, a refusal included (an internal server error
  is the exception): the `Deprecation` header
  (RFC 9745) carries the day it was deprecated, as `@` and the Unix time (for example
  `Deprecation: @1803859200` for 1 March 2027), and when the day of its removal is known, the
  `Sunset` header (RFC 8594) carries it as an HTTP date (`Sunset: Wed, 01 Sep 2027 00:00:00
  GMT`). The OpenAPI document has the same days as `x-deprecated-since` and `x-sunset`.
- **It is announced** in the changelog of the release that deprecates it and in its upgrade notes,
  with what to use instead.

Nothing is deprecated today.

## Where to get the document

The OpenAPI 3.1 document of the platform API is in three places:

- **On this site:** [`openapi.json`](openapi.json), for the documented release.
- **On each release:** the file `lakana-openapi.json` on the release's page on
  [GitHub](https://github.com/lakana-labs/lakana/releases), with the release's version in
  `info.version`.
- **On your instance:** the server answers `GET /openapi.json`, without signing in, and shows the
  same document as pages at `/docs`. That document also lists the Iceberg REST catalog's routes,
  and has no version of its own.

Every route in the document has a short summary and a description written for the people who
call it: who may call it, what it does and the main refusals with their status codes. The error
answers (`401`, `403`, `404`, `409` and others) are listed with the routes that return them, each
with the body `{"detail": "...", "type": "..."}`. A change of that wording is not a change of the
contract. The reference pages of this site ([Reference](index.md)) describe the routes in more
depth.

## How Lakana keeps the promise

The document is generated from the code and committed with it. Every pull request compares its
copy with the one on the branch it merges into: a breaking difference needs the change to be
marked breaking, so it cannot land by accident, and an additive one only updates the file.
