# How-to guides

A how-to guide solves one task by the shortest path, for someone who knows what they want.

Pages:

- [Projects and deploys](projects.md): start a project, deploy it, run it, set up CI and write a first Python asset. The layout, the keys and the rules are in the reference.
- [Load data from a source](load-data-from-a-source.md): read files, a SQL database or a REST API into bronze with a short asset from `lakana init --template`, run it on a schedule, and what each error means.
- [Read from a SQL Server database](read-from-a-sql-server-database.md): a recipe for a Python asset that reads Microsoft SQL Server with the `python-tds` driver through a `tcp` connection, with TLS, the type pitfalls, what each error means and what was tested.
- [Deploy from GitHub Actions without a stored secret](deploy-from-github-actions.md): let a workflow exchange its OIDC ID token for a one-deploy token, with the trust rules, the workflow and what to do when the exchange is refused.
- [Deploy from GitLab CI without a stored secret](deploy-from-gitlab-ci.md): the same for a GitLab pipeline, and how one job submits twice.
- [Check your data](check-your-data.md): state in SQL what must be true of a table, and what happens to the table when it is not.
- [Run a job on a schedule](run-a-job-on-a-schedule.md): declare a job and its cron schedule, bind a run principal, pause it, start it from CI, and what happens when the project is busy or the platform was down.
- [Get told when a job fails](get-told-when-a-job-fails.md): send a message to your chat tool or a webhook when a run fails and when the job works again, test it, and verify its signature.
- [Deploying with Compose (team mode)](deploy-with-compose.md): run a team instance with Docker Compose.
- [Run Lakana for other people](operate-for-others.md): install, hand over, back up, upgrade, watch and separate instances, in the order an operator needs them.
- [Is my instance healthy?](instance-health.md): what to look at (services, workers, queue, database, storage, disk), which log lines matter, and what to collect before you ask a question.
- [Give a second person access](give-a-second-person-access.md): create a user, grant the smallest set to query one environment, issue a token, and take access away.
- [Why may (or may not) someone do this?](why-may-someone-do-this.md): see what a principal may do on an object, and through which grant, role or group.
- [Upgrade to the release with artifacts](upgrade-to-artifacts.md): the migration, the order and the way back.
- [Manage secrets](manage-secrets.md): see how each stored secret is encrypted and set an older one again.
- [Use a private package index](private-package-index.md): install your team's own package in Python assets, with a password that stays out of the project.
- [Run Python tasks in containers](run-python-tasks-in-containers.md): what a container per task isolates, how to set it, and what each message means.
- [Train a scikit-learn model and score with it](models-with-scikit-learn.md): store a model as an artifact and read it in another asset.
- [Inspect and download artifact versions](inspect-and-download-artifact-versions.md): see what an artifact holds and copy a version to your machine.
- [Limit how many versions are kept](limit-artifact-versions.md): keep only the newest versions of an artifact.
- [Delete an artifact version](delete-an-artifact-version.md): remove one version or a whole artifact; Lakana cannot undo it.
- [Show some people only some rows](filter-rows.md): hide the rows that fail a condition, and what Lakana refuses when you create a filter.
- [Mask a column for some people](mask-a-column.md): show a replacement value instead of a column, and what masks do not protect.
- [Declare a table's model](declare-a-tables-model.md): say in an asset's header what a table is, what one row stands for, its key and the tables it points at; get checks from it, and see what each message means.
- [Keep layers apart](keep-layers-apart.md): write down which layers each layer may read, how its tables are written and which kinds they declare; every deploy checks them.
- [Describe a table](describe-a-table.md): write a description for a table and its columns, in the project or by hand, and go back to the project's text.
- [Translate labels and descriptions](translate-labels-and-descriptions.md): show a table's labels and descriptions in more languages, read them in yours, and correct a translation by hand.
- [Drop and restore a table](drop-and-restore-a-table.md): drop a table, restore it with its data and access rules during the undo period, or delete its files now; delete and restore a whole environment the same way.
- [Clean up files left by older releases](clean-up-files-left-by-older-releases.md): list the table directories in storage that an older release left behind without a record, and delete them.
- [Let tables expire](let-tables-expire.md): let a development table, or every table of an environment, be dropped once nobody has used it for a while; see what expires soon, and keep a table.
- [Find a table](find-a-table.md): search tables, columns, descriptions, runs and people from one text, and what you are allowed to see.
- [Find a run, a query or a deploy](find-a-run.md): filter the lists by status, trigger, job, time, submitter or repository, and page through a long one.
- [Stop a query](stop-a-query.md): cancel one with Ctrl-C or a command, who may, and what else ends a query.
- [Why is my query or task waiting?](why-is-it-waiting.md): read the reason a query or a task has not started and what to do about it.
- [List and withdraw an open upload](withdraw-an-artifact-upload.md): free the place that an upload left by a killed process holds.

Pages are added here as they are written.
