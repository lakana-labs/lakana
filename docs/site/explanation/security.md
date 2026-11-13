# Security overview

How Lakana decides who may do what, how a person signs in to the web interface, where it keeps
secrets, what it records, and how a query is kept inside what its author may read. This page is the short version for people who use or run an
instance; each part links to the page with the steps and the exact rules.

## Who may do what

Every request is made by a **principal**: a person or a service account, signed in with a token
or, in the web interface, with a browser session.
A principal may do something only because a **grant** says so. A grant gives an action, or a
**role** (a named set of actions such as `VIEWER`, `ANALYST`, `DATA_SCIENTIST`, `DATA_ENGINEER` or
`ADMIN`), to a principal or a group, on an object: the instance, an environment, a namespace or a
table. Without a grant the answer is no, and the refusal is recorded.

- **Give the smallest set.** A role on an environment covers every table in it, tables created
  later included. [Give a second person access](../how-to/give-a-second-person-access.md) shows the
  grants for a reader, and how to take access away.
- **Ask why.** [Why may (or may not) someone do this?](../how-to/why-may-someone-do-this.md) names the
  grant, role or group behind an answer. The lists and the actions are in
  [Grants, permissions, principals and groups](../reference/access.md).
- **Row filters** show a person only the rows that meet a condition. **Column masks** show a
  replacement value instead of a column. Both are applied inside every query the person runs, a join, a
  view and a write into another table included, and they follow a person or a group. Create them
  before you give access: [Show some people only some rows](../how-to/filter-rows.md) and
  [Mask a column for some people](../how-to/mask-a-column.md).
- **CI jobs need no stored token.** A GitHub Actions or GitLab CI job can exchange the OIDC ID
  token its provider gives it for a Lakana token that may queue one deploy of the job's own commit
  to one environment, when a CI trust rule an administrator wrote names its repository and branch
  or environment. The token lasts an hour and still needs the service account's grant
  ([CI trust rules and CI tokens](../reference/ci-trust-rules.md)).
- **Protected environments** accept writes only from deployed projects, so a person cannot change a
  production table by hand ([Projects and deploys](../how-to/projects.md#protected-environments-lockdown-and-break-glass)).

## Signing in to the web interface

A person signs in to the interface with a **passkey**: a key pair whose private half stays on
their device or in their password manager, unlocked with a fingerprint, a face, a PIN or a
security key. Lakana stores only the public half. A link signs a person in only to start: an
invite creates the account, a recovery link lets them back in, and both lead straight to adding a
passkey ([Invite your team](../how-to/invite-your-team.md),
[Lost your passkey](../how-to/lost-your-passkey.md)).

- **A session is a credential, like a token.** Signing in gives the browser a session cookie that
  scripts cannot read (`HttpOnly`), that the browser sends only to the same site (`SameSite=Strict`)
  and, over HTTPS, only over HTTPS. Every request with it carries the person's own rights, no more:
  the same grants, filters, masks, limits and audit as their token would. Every request, reads
  too, also carries a second secret that only the page holds, in its own address's storage
  (address and port): the cookie alone signs nobody in, and another site cannot make the browser
  act. Signing out removes it; a new tab of the same address keeps working. The session is
  checked on every request, so ending it takes effect at once.
- **Local mode is for a machine only you use.** On `http://localhost`, browsers send the cookie
  to every program serving another `localhost` port, because cookies do not separate ports.
  Without the page's second secret such a program cannot act as you, but it can still sign you
  out. The start also opens a sign-in link in your browser: it works once and only for one
  minute, but on Linux other accounts on the machine can see it in the list of running programs
  while the browser starts, and then in the browser's own entry there, by which time it is used
  or has expired. Do not run `lakana server start --local` on a machine shared with other
  people's accounts; there, run the Compose bundle with HTTPS
  ([Deploy with Compose](../how-to/deploy-with-compose.md)).
- **A session ends** after 12 hours without anything the person did, after 7 days at the latest
  (an administrator may change both in **Settings → Sign-in**, up to 24 hours and 30 days), when the
  person signs out here or everywhere, when an administrator ends it, and when the account is
  deactivated. Background refreshes of a page never keep it alive.
- **Sensitive actions need a fresh touch.** In a browser, actions that change who may do what or
  hand out credentials need a passkey touch from the last 15 minutes; the browser asks for one when
  it is older. They are: adding or removing a passkey; issuing an invite or a recovery link;
  creating an API token; creating or deactivating a person; granting or revoking rights, group
  memberships, row filters and column masks, and applying an access file; ending another person's
  sessions; changing the session lengths; creating or changing secrets, connections, CI trust
  rules, repositories and package indexes; creating an environment or changing its protection or
  storage; and binding a run-as account to a deploy. A stolen session cookie alone is therefore
  not enough for any of them. A request with an API token needs no touch: the token is what the
  command line uses.
- **A passkey is bound to the address.** The browser offers a passkey only to the site it was made
  for, and Lakana accepts its answer only from its own exact address, port included, so a
  look-alike site gets nothing. Changing the public address
  makes every passkey unusable ([Lost your passkey](../how-to/lost-your-passkey.md#after-changing-the-public-address)).
  A passkey that turns out to have been copied (its counter goes backwards) is disabled, every
  session of its owner ends, and the owner and the administrators get a notice.
- **A new link or token is shown once.** The interface shows an invite or recovery link, and
  later an API token a person creates, exactly once and never stores it; the server keeps only a
  hash. The interface itself never uses a token and never sees an existing one.

## How Lakana meets MFA requirements

Many teams must show that people sign in with more than one factor. This is what Lakana does; it
makes no claim of certification or compliance with any standard.

- **A passkey is multi-factor in one step.** Lakana requires user verification, so a passkey
  signs in only with something the person has (the device or security key holding it) together
  with something they are or know (a fingerprint, a face or the device's PIN). It is bound to
  Lakana's exact address, so it resists phishing: a look-alike site cannot ask for it, and an
  answer meant for another address is refused.
- **Single sign-on, when it comes,** will leave the factors to your identity provider: a person
  signing in through it gets whatever multi-factor rule the provider enforces.
- **API tokens are a single factor, by design.** Scripts, CI jobs and the command line use them.
  The server stores only a hash of each token; any token can be revoked at once; CI and task tokens
  expire on their own (a CI token after an hour). A task token, a CI token and a service
  account's token never lead to a browser session. A person's own token signs its holder into a
  browser only through `lakana server start --local`, on the machine that runs local mode. Two
  kinds of token reach further, and need the same care as the passkeys themselves:
    - **The server operator's bootstrap token** issues the first administrator's setup link and
      recovery links for any person, administrators and its own account included, at any time.
      Revoking it closes the setup links and administrators' recovery links it issued: they are
      refused when they are opened.
    - **An administrator's personal token** issues invite and recovery links (for anyone but
      administrators) without a passkey touch, as the command line does, and can grant rights the
      same way. Whoever holds it can therefore sign into a browser as another person, or as a new
      account they then grant rights to. Keep administrators' tokens as safe as the bootstrap
      token, give them to as few people as possible, and revoke one that is not in use.

  A browser session in turn never creates an API token without a fresh passkey touch.
- **Recovery links are the known weak point,** because they let someone in without a passkey.
  Lakana limits them: a link works once, for 1 hour; using it removes all of the person's passkeys
  and ends their sessions, so it cannot be used quietly alongside the real owner; every
  administrator gets a notice; and an administrator's account is recovered only by the server
  operator, never by another administrator. That holds for anyone who has ever held the `ADMIN`
  role, directly or through a group: taking the role away first does not let another
  administrator recover them. Confirm a request outside Lakana before you send a link.
- **A link stands only while its issuer does.** Deactivating the administrator who issued an
  invite or a recovery link, or taking their `ADMIN` role away (revoking the grant, or removing
  them from the group that gives it), withdraws their open links at once, and every link is
  checked again when it is used: the person is told why and to ask an administrator for a new
  link. So deactivating an administrator whose browser or token was stolen also ends the links
  the thief issued. The server operator's links need the bootstrap account active and holding
  `ADMIN`, like anyone's.

## Where secrets live

A secret (a storage key, a repository token, a package index password, a webhook address, a
connection's password) is stored encrypted in the instance's database, under a master key that you
set with `LAKANA_SECRETS__MASTER_KEY`. No command or page returns its value again, and no route
returns it to a person; to change it you set it again. A secret is **bound** to one use, for
example one package index, one repository or one connection, and Lakana refuses it for anything
else, so the person who registers an index cannot send a secret to a host of their choice. A secret
is decrypted only where it is used, and only while it is used.

The one answer that carries a secret's values goes to a running task, for the connection secret of
a connection its asset declares ([How a task gets its credential](../reference/connections.md#how-a-task-gets-its-credential)).
The server answers only the token of a task that is running now, in its own environment, for a name
its deployed asset declares; it answers an undeclared name the same way whether or not a
connection of that name exists. The task keeps the credential in memory for the attempt, never in
an environment variable or a file, and each value is shown as `***` in the task's log, its
failure message and its trace, also in its common encodings (a `repr`, JSON, a URL); a value
shorter than 4 characters, another transformation of it, or a value derived from it (such as an
OAuth2 access token) is not hidden unless a Lakana helper registers it (`files.read` and
`rest.read` register the values they derive). A worker that cannot decrypt a declared connection's secret when the
attempt starts does not start it; a secret
added, bound or rotated while an attempt runs may appear in the log the worker captures, so
change secrets between runs. Every fetch is in the audit log; refusals are recorded
at a limited rate per principal and the rest counted. The code you
deploy can read the credentials of the connections its asset declares and send them anywhere the
task can reach (in a container, those connections' endpoints; with the local runner, anywhere), so
review an asset's `connections` with its code.

Keep the master key and the `.env` file of a Compose bundle with the people who run the instance: whoever
holds them holds every secret, and a lost master key makes every stored secret unrecoverable. The steps
are in [Manage secrets](../how-to/manage-secrets.md) and
[Use a private package index](../how-to/private-package-index.md).

## What the audit log records

The audit log is an append-only list of events: who signed in or failed to, what was granted
and revoked, who created, rotated or deleted a secret, who changed an environment, a filter or a mask,
who deployed, ran or undid something, and every refusal. A completed query is recorded with its SQL
text. For that reason reading the log needs its own right, `READ_AUDIT_LOG`, which the `ADMIN` role
holds, and seeing query text and column values in **other people's** events (the SQL text of a
query, the watermark of an incremental write) needs one more, `READ_QUERY_TEXT`, which no role
holds: an administrator sees a query's SHA-256 and length instead, and of a watermark only that
it is hidden, until someone grants the right to them by name. Your own events always show in full. An operator can also store less
text in new events (`LAKANA_AUDIT__QUERY_TEXT`: `full`, `hash` or `off`); see
[Other people's query text and column values](../reference/lists.md#other-peoples-query-text-and-column-values-in-the-audit-log).
`lakana audit list` filters it by event type, kind (`--kind connection`), environment, principal and time
([the command](../reference/cli.md#lakana-audit-list)). To keep the log bounded, repeated refusals of one
principal are counted after a number per minute that an operator sets
(`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`, [settings](../reference/settings.md)).
For the same reason a successful sign-in is recorded once per token every 15 minutes, not on every
request: a screen that refreshes itself every few seconds would otherwise write thousands of
`AUTHENTICATION_SUCCEEDED` events a day. The next event of the token says in `detail.suppressed` how
many sign-ins were left out. Each token counts on its own, and its first use, a new token and the
first use after a refused attempt always write an event. Each server process counts on its own, so
a deployment with two server processes can write two events per window. Failed sign-ins are
recorded as before. `LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES` sets the window (0 records every
request; [settings](../reference/settings.md)).

Seeing other people's activity is recorded too. When a manager lists other people's queries
(`lakana query list`), an administrator sees who read a table (`lakana table health`), or someone
reads other people's events in the audit log itself (`lakana audit list`), the log gets
an `ACTIVITY_VIEWED` event: who looked, at which environment or table, with which filters, and how many
rows of how many other people the answer showed, never their names or their SQL. It is written once per
person and view per hour, so paging, other filters and a screen that refreshes itself add nothing; a
filter on one person (`--submitter`) is its own view. Looking only at your own activity records nothing.
If the event cannot be written, the read is refused rather than served unrecorded. Reading the text
of the file that defines a table writes a `SOURCE_VIEWED` event (the repository, path and commit),
once per person, table and commit per hour, and is refused too if that event cannot be written. The events and
the setting that turns them off are in [the lists reference](../reference/lists.md#audit).

## How a query is kept inside its limits

Policies are applied by rewriting the SQL a person submits, but Lakana does not rely on the rewrite
alone, and it does not try to forbid dangerous DuckDB functions one by one. A query is kept inside
what its author may read by **credential scoping**:

- The worker works out, from the catalog, the files the author may read, and gives DuckDB a
  time-limited link to exactly those files.
- The process that runs the query holds no storage credential, no database address and no token,
  and it opens only the locations of the tables the query reads (in local mode, their directories).
  A query that reads another path or address is refused, and one it was not given a link for has
  nothing to read it with.
- Limits on time, memory, rows and bytes stop a query that asks for too much
  ([Stop a query](../how-to/stop-a-query.md)).

A query only reads. It is one `SELECT` (also with `WITH`, or a set operation such as `UNION`), or
a `VALUES` list such as `VALUES (1, 'a'), (2, 'b')`, whose columns are named `col0`, `col1` and
so on. A statement that changes data or the session is refused wherever it sits, also inside a CTE
or a subquery, and so is a query that reads files directly instead of a table: register the data
as a table and select from it. A `PIVOT` lists the values of its `ON` column, as in
`SELECT * FROM (PIVOT gold.sales ON region IN ('eu', 'us') USING sum(amount))`; without the list
it is refused with `A PIVOT needs the values of its ON column listed`, because DuckDB would run it
as two statements.

## What this does not protect

- **Whoever holds a link before its person does.** An invite or recovery link signs in whoever
  opens it first. Send links on a channel you trust and confirm the person outside Lakana before
  you grant a role. The page takes the link out of the address bar and out of the tab's Back and
  Forward steps before it sends anything, but the browser's own history (Ctrl+H, and the
  suggestions of its address bar) keeps the address as it was first opened: Chrome and Firefox
  both do, and a page cannot change that. Opening an invite or recovery link only shows it; it is
  used when the person chooses **Continue**. Until then, a link in that history still works: an
  invite for up to 7 days, a recovery link for up to 1 hour. Once used, expired or withdrawn it is
  worthless; the local start's link lasts one minute, used or not. Withdraw a link that was
  opened but not used, and clear that history entry if others use the device or see the screen.
- **A device that is unlocked and signed in.** A session is as good as the person at the keyboard
  until it ends; the sensitive actions above still need a passkey touch.
- **Personal API tokens do not expire by themselves.** Revoke a token that is no longer used; the
  token list shows when each was last used.
- **Under the local runner, one instance is one trust domain for Python code.** A Python task runs as
  the worker's operating system user, so someone who may deploy Python code can read what the worker
  reads, filters and masks included. Deploy Python only from people you would trust with the stored data,
  or run one instance per group that does not trust the other
  ([why](python-assets.md#isolation-one-instance-is-one-trust-domain)). The same holds for whoever may
  manage connections: with the local runner, a connection test dials from the worker's host to any
  address the connection names, so a data engineer without the right to deploy can probe which ports and
  ingestion-root paths the worker can reach. With the Docker runner a connection test runs in a
  container under the network rules a task with that connection gets, so it reaches only what such a
  task could, on Lakana's own check and the task image's check environment (its drivers only,
  owned by root and read-only), never a project's code. A connection test logs in with the
  connection's secret, so whoever may test a connection makes it send its credentials to the
  connection's own address. A `gcs` key is the exception that is narrowed further: its signed token
  request goes only to Google's token endpoint, and a key that names another is refused before
  anything is sent. With the docker runner, the
  Compose bundle's default, each task runs in a container without the worker's credentials, and
  reaches the server and the endpoints of the connections its asset declares, nothing else;
  containers share the host's kernel, so this is not a sandbox for hostile code
  ([Run Python tasks in containers](../how-to/run-python-tasks-in-containers.md)).
- **A declared connection's host gets what the task sends it.** A task can send its declared
  connections' credentials, and any data it reads, to those connections' endpoints. With
  containers, an address of the Docker host that you do not list in `LAKANA_EGRESS_REFUSED` is
  reachable when a connection names it
  ([Addresses builds and tasks never reach](../reference/network-settings.md#addresses-builds-and-tasks-never-reach)).
- **Files read outside Lakana.** A filter or a mask guards Lakana's own query path, not the files in
  the object storage. Keep the storage credentials with the people who run the instance.
- **`lakana load` on macOS and Windows.** On Linux the command reads a Parquet or Arrow file in a
  process whose memory is capped, so a crafted file is refused as too large. Other systems do not
  enforce that cap, and such a file can take far more of the command's memory than the upload limit
  before it is refused ([`lakana load`](../reference/load.md)). Load files from people you do not
  know on Linux, or with an asset.
- **Local mode.** With storage on the local disk there is no cloud credential to scope, so a local
  instance relies on the other checks alone. Use a team deployment ([Deploying with Compose](../how-to/deploy-with-compose.md))
  for more than one person.

For the connections an instance makes, see [What leaves the machine](what-leaves-the-machine.md).
