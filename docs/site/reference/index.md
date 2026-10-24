# Reference

Reference states exact facts, to look up, and does not teach.

Pages:

- [Artifacts](artifacts.md): the `@asset` options, limits, commands, SDK, REST routes, permissions and audit events of the artifact store.
- [Package indexes](package-indexes.md): the record, the sources a lock may name, commands, SDK, REST routes, permissions and audit events.
- [Connections](connections.md): the kinds of source, their address, settings and secret keys, the limits, who may do what, commands, REST routes, SDK, audit events and errors.
- [CI trust rules and CI tokens](ci-trust-rules.md): which CI jobs may deploy with their OIDC ID token, the exchange, what a CI token may do, refusals, commands, SDK, routes and audit events.
- [Checks](checks.md): the files, the keys, every refusal at deploy, the outcomes, commands, routes, permissions and audit events of data quality checks.
- [Descriptions](descriptions.md): the two sources of a table's and a column's description, commands, SDK, REST routes, rights and audit events.
- [Merging rows into a table](merge.md): the key, the counts, the first merge, the command, the SDK, the REST route, who may merge, every refusal, the audit event and the limits.
- [Table health](table-health.md): the numbers of a table's snapshot, its last maintenance, the newest result of each check and who reads it; the fields, the rules, the settings, commands, SDK and routes.
- [Search](search.md): what is matched, the ranking, who sees what, the route, the command, the SDK and how the columns of existing tables are filled in.

- [Runs, queries and deploys lists](lists.md): the filters, the order and the cursor of the three lists, the refusals, commands, SDK and routes.
- [Grants, permissions, principals and groups](access.md): the grants list and its filters, a principal's permissions and how they are decided, searching principals and groups, the refusals, commands, SDK and routes.
- [Why did it fail?](why-did-it-fail.md): the stable failure codes of a task, what each means and what to do, where they appear and what old runs show.

- [Jobs](jobs.md): the triggers of a job, the fields of the job listing, routes, commands and SDK.

- [Command line](cli.md): every `lakana` command and sub-command with its arguments and options. Generated from the code.
- [Settings](settings.md): every `LAKANA_*` environment variable with its type, default, reader and description. Generated from the code.
- [The REST API as a contract](rest-api.md): the platform API and the Iceberg REST catalog, what Lakana promises, what counts as breaking, how deprecations are announced, and where to get the [OpenAPI document](openapi.json) (generated from the code).

- [SQL in queries](sql-in-queries.md): lambdas, a column that is only `NULL`, and the functions a query may not call, with the message and the next step for each.
- [Project layout](project-layout.md): the files a deploy reads and the table names they give.
- [Asset front-matter](asset-front-matter.md): every key of the `/* lakana */` block of an asset.
- [`lakana.yaml`](lakana-yaml.md): defaults, requirements, jobs, notifications, check defaults, retries and per-environment overrides.
- [Python assets](python-assets.md): the rules, what a deploy checks, what a task may do, failures and the operator's settings.
- [Network settings of the worker](network-settings.md): git and webhook proxies and CA files.
- [The Compose bundle](compose-bundle.md): the keys of `.env`, the services and the settings an operator changes most.
- [Log events](log-events.md): the stable codes (`event=<code>`) of the log lines worth an alert, what each means and what to do.

Pages are added here as they are written.
