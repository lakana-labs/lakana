# How-to guides

A how-to guide solves one task by the shortest path, for someone who knows what they want.

## Load data

- [Load data from a source](load-data-from-a-source.md): read files, a SQL database or a REST API into bronze with a short asset from `lakana init --template`, run it on a schedule, and what each error means.
- [Read from a SQL Server database](read-from-a-sql-server-database.md): a recipe for a Python asset that reads Microsoft SQL Server with the `python-tds` driver through a `tcp` connection, with TLS, the type pitfalls, what each error means and what was tested.
- [Use a private package index](private-package-index.md): install your team's own package in Python assets, with a password that stays out of the project.
- [Run Python tasks in containers](run-python-tasks-in-containers.md): what a container per task isolates, how to set it, and what each message means.

## Model data

- [Declare a table's model](declare-a-tables-model.md): say in an asset's header what a table is, what one row stands for, its key and the tables it points at; get checks from it, and see what each message means.
- [Keep layers apart](keep-layers-apart.md): write down which layers each layer may read, how its tables are written and which kinds they declare; every deploy checks them.
- [Model a star schema](model-a-star-schema.md): start from `lakana init --template medallion`, a bronze, silver and gold project with a fact, dimensions, history, a wide table, a glossary and German translations, and see what each choice does.
- [Keep the history of a dimension](keep-the-history-of-a-dimension.md): keep every version of a dimension's rows, and join each fact to the version valid when it happened.
- [Describe a table](describe-a-table.md): write a description for a table and its columns, in the project or by hand, and go back to the project's text.
- [Translate labels and descriptions](translate-labels-and-descriptions.md): show a table's labels and descriptions in more languages, read them in yours, and correct a translation by hand.
- [Keep a glossary](keep-a-glossary.md): keep your team's words as YAML files next to the data, so a search for a word finds the term, what it means, who answers for it and the columns it points to.

## Check data quality and health

- [Check your data](check-your-data.md): state in SQL what must be true of a table, and what happens to the table when it is not.
- [Notice a broken pipeline](notice-a-broken-pipeline.md): read the health signals Lakana records for every table, and make one stop a run.
- [Find what a change breaks](find-what-a-change-breaks.md): before you rename or remove a column, see the references, checks, measures, glossary terms, policies and jobs of every repository that depend on it.
- [Get told when a job fails](get-told-when-a-job-fails.md): send a message to your chat tool or a webhook when a run fails and when the job works again, test it, and verify its signature.

## Deploy and schedule

- [Projects and deploys](projects.md): start a project, deploy it, run it, set up CI and write a first Python asset. The layout, the keys and the rules are in the reference.
- [Connect a repository](connect-a-repository.md): let Lakana make a deploy key for each environment, add the public keys at your git host, and deploy from the repository over SSH.
- [Move a SQL project to Lakana](move-a-sql-project.md): convert a small project of SQL files by hand into a Lakana project, deploy it to your own machine, run it twice and query the result.
- [Deploy from GitHub Actions without a stored secret](deploy-from-github-actions.md): let a workflow exchange its OIDC ID token for a one-deploy token, with the trust rules, the workflow and what to do when the exchange is refused.
- [Deploy from GitLab CI without a stored secret](deploy-from-gitlab-ci.md): the same for a GitLab pipeline, and how one job submits twice.
- [Run a job on a schedule](run-a-job-on-a-schedule.md): declare a job and its cron schedule, bind a run principal, pause it, start it from CI, and what happens when the project is busy or the platform was down.
- [Find a run, a query or a deploy](find-a-run.md): filter the lists by status, trigger, job, time, submitter or repository, and page through a long one.
- [Stop a query](stop-a-query.md): cancel one with Ctrl-C or a command, who may, and what else ends a query.
- [Why is my query or task waiting?](why-is-it-waiting.md): read the reason a query or a task has not started and what to do about it.

## Access and security

- [Give a second person access](give-a-second-person-access.md): create a user, grant the smallest set to query one environment, issue a token, and take access away.
- [Invite your team](invite-your-team.md): invite a colleague to the web interface by a link, confirm them before granting a role, recover a lost passkey, and end a person's sessions.
- [Lost your passkey](lost-your-passkey.md): get back into the web interface with a recovery link, issue one as an administrator, recover an administrator as the server operator, and what changing the public address does to passkeys.
- [Keep access in a reviewed file](keep-access-in-a-reviewed-file.md): export who has access to what as a file, review changes in a pull request, apply them from CI, and what a managed scope refuses.
- [Why may (or may not) someone do this?](why-may-someone-do-this.md): see what a principal may do on an object, and through which grant, role or group.
- [Show some people only some rows](filter-rows.md): hide the rows that fail a condition, and what Lakana refuses when you create a filter.
- [Mask a column for some people](mask-a-column.md): show a replacement value instead of a column, and what masks do not protect.
- [Manage secrets](manage-secrets.md): see how each stored secret is encrypted and set an older one again.

## The catalog and the web interface

- [Start in the web interface](start-in-the-web-interface.md): what Home's boxes tell you, what Healthy, Degraded and Down mean, the Get started list and hiding it, and the update note after an upgrade.
- [Find a table](find-a-table.md): search tables, columns, descriptions, runs and people from one text, and what you are allowed to see.
- [Open a table](open-a-table.md): open a table's page from the Catalog or from Home, read its columns and preview, and see what the page says when you may not read the table.
- [Preview a table](preview-a-table.md): look at the first rows of a table in one command or on its page in the web interface, without writing SQL, and what a preview hides.
- [See where a table is defined](see-where-a-table-is-defined.md): find the file, line and commit in git that define a table, and read that file in the terminal or the web interface.
- [Drop and restore a table](drop-and-restore-a-table.md): drop a table, restore it with its data and access rules during the undo period, or delete its files now; delete and restore a whole environment the same way.
- [Let tables expire](let-tables-expire.md): let a development table, or every table of an environment, be dropped once nobody has used it for a while; see what expires soon, and keep a table.
- [Organise tables in namespaces](organise-tables-in-namespaces.md): create a namespace, see them, and delete an empty one, on the command line or in the Catalog, also in a protected environment.
- [Register files as an external table](register-files-as-an-external-table.md): make files that already lie under `raw/` readable as a table, the rules for its location, its formats, and what each refusal means, on the command line or in the Catalog.

## Run an instance

- [Deploying with Compose (team mode)](deploy-with-compose.md): run a team instance with Docker Compose.
- [Run Lakana for other people](operate-for-others.md): install, hand over, back up, upgrade, watch and separate instances, in the order an operator needs them.
- [Is my instance healthy?](instance-health.md): what to look at (services, workers, queue, database, storage, disk), which log lines matter, and what to collect before you ask a question.
- [Export and keep the audit log](export-and-keep-the-audit-log.md): what the log records and does not, how to export it on a schedule, who may read it, and that it has no tamper-evidence yet.
- [Install without internet access](install-without-internet-access.md): packages, the container image, an internal package index for Python assets, git on an internal server and the DuckDB extension.
- [Upgrade to the release with artifacts](upgrade-to-artifacts.md): the migration, the order and the way back.
- [Clean up files left by older releases](clean-up-files-left-by-older-releases.md): list the table directories in storage that an older release left behind without a record, and delete them.

## Models and artifacts

- [Train a scikit-learn model and score with it](models-with-scikit-learn.md): store a model as an artifact and read it in another asset.
- [Inspect and download artifact versions](inspect-and-download-artifact-versions.md): see what an artifact holds and copy a version to your machine.
- [Limit how many versions are kept](limit-artifact-versions.md): keep only the newest versions of an artifact.
- [Delete an artifact version](delete-an-artifact-version.md): remove one version or a whole artifact; Lakana cannot undo it.
- [List and withdraw an open upload](withdraw-an-artifact-upload.md): free the place that an upload left by a killed process holds.

Pages are added here as they are written.
