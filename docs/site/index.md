# Lakana

Lakana is an open-source data lakehouse for small and medium teams: governed and reproducible
by default, simple enough to run on one machine, professional enough to run an organisation's
data platform. It covers analytics, data science and ML in SQL and Python, with code in git and
every table, feature and model traceable to a commit. It runs anywhere containers run, and scales
by adding workers, not by distributing queries.

New here? Begin with [Getting started](tutorials/getting-started.md).

There is no web interface yet. You work with Lakana through the command line (CLI), the Python
SDK or the REST API, and the interface is planned before the public beta
(see the [roadmap](explanation/roadmap.md)). To build on the REST API, read [The REST API as a
contract](reference/rest-api.md): what Lakana promises about it and where to get its OpenAPI
document.

Lakana is an open-source project with best-effort [support](explanation/support.md), and its
code contains no telemetry: see [what leaves the machine](explanation/what-leaves-the-machine.md).

The documentation has four kinds of page, and each page is only one kind:

- [Tutorials](tutorials/index.md) teach by doing, from start to finish, for someone new.
- [How-to guides](how-to/index.md) solve one task by the shortest path.
- [Reference](reference/index.md) states exact facts, to look up.
- [Explanation](explanation/index.md) says why things are the way they are.
