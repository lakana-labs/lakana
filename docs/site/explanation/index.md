# Concepts

These pages explain how Lakana works and why: what it is for, how its parts fit together, and what was weighed.

Pages:

- [Scope and philosophy](scope.md): what Lakana is for, what it is not, and who it is for.
- [Where Lakana fits, and why Apache Iceberg](where-lakana-fits.md): what Lakana adds to a stack you assemble yourself, what it lacks, why Apache Iceberg and what that costs, and when to stay with what you have.
- [Moving SQL transformations to Lakana](moving-sql-transformations.md): what a model, a reference, a test, a source, a seed and a target become, what has no counterpart, and when to stay with what you have.
- [What leaves the machine](what-leaves-the-machine.md): the outbound connections Lakana makes.
- [Artifacts: what they are and what they are not](artifacts.md): what a stored model is, who can read and write it, and what deleting removes.
- [Python assets: what they are for and how far they are trusted](python-assets.md): why SQL first, and why one instance is one trust domain.
- [When a table's columns change](when-columns-change.md): what `evolve_schema` does to a table, and what a removed column means for queries, views, downstream assets and old snapshots.
- [Security overview](security.md): who may do what, where secrets live, what the audit log records and how a query is kept inside its limits.
- [How Lakana looks and why](how-lakana-looks.md): the look of the web interface, its tokens, status glyphs and patterns, and what was weighed; for anyone who changes a screen.

Pages are added here as they are written.
