# What is Lakana?

Lakana is an open-source data lakehouse for small and medium teams: governed and reproducible
by default, simple enough to run on one machine, professional enough to run an organisation's
data platform. It covers analytics, data science and ML in SQL and Python, with code in git and
every table, feature and model traceable to a commit. It scales by adding workers, not by
distributing queries.

## The problem

A team can assemble a data platform from parts: table files, a query engine, a scheduler. What
keeps several people, and production, safe is then its own to add: access rules, an audit log,
lineage and a production that only deployed code can change.

## The idea

A lakehouse keeps data as open table files on inexpensive storage and puts a catalog, access
control and a query engine on top. Lakana packages those parts, built on DuckDB, Apache Iceberg
and PostgreSQL, so that a small team gets them in one install instead of assembling them.

## What Lakana adds

- **Governed by default:** catalog, row and column policies, secrets, audit and lineage in one
  install.
- **Reproducible:** production runs only deployed, versioned code.
- **Two languages:** SQL and Python, nothing else.
- **Self-hosted:** from a laptop to a cluster, with no hosted service.

## Who it is for

- One person or a small team responsible for an organisation's data, who works with git, SQL
  and Python.
- Those who run Lakana for others.
- Individuals, and teaching.

The size of the organisation does not decide the fit; the skills of the people who run it do.
It is not for petabyte scale or queries across many nodes, for teams that want a hosted service,
or for R-first groups.

## Where it stands

Lakana is pre-1.0: the API and the data layout may change between releases, and it is not ready
for production data until v1.0.0. You work with it through the command line, the Python SDK or
the REST API. The web interface is under way: a first part exists, off by default.

Lakana is open source, with best-effort [support](explanation/support.md), and its
code contains no telemetry: see [what leaves the machine](explanation/what-leaves-the-machine.md).
More in [Scope and philosophy](explanation/scope.md) and
[Where Lakana fits](explanation/where-lakana-fits.md).

## Two ways in

- [Start learning](learn/index.md): from your first instance to a trained model.
- [Find a how-to](how-to/index.md): one task by the shortest path.
