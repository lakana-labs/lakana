# Connect a repository

Lakana deploys a project from its git repository over SSH, with a **deploy key**: an SSH key
that may read that one repository. Lakana makes the key for you, one key pair per environment,
and keeps each private key as that environment's secret. You never see or copy a private key;
you add the public keys at your git host.

You need the ADMIN role (or `MANAGE_REPOSITORY` on the instance and `MANAGE_SECRET` in each
environment), a running worker that takes pipeline work, and the repository's SSH URL
(`git@host:team/repo.git` or `ssh://git@host/team/repo.git`).

## In the web interface

An administrator opens **Connect a repository** from **Settings → Repositories**, from Home's
**Get started** list, or at `/settings/repositories/new`. The page has four numbered steps; each appears once the one
before it is done.

1. **The repository.** Paste the **Repository URL**, the one you clone it with. For GitHub,
   GitLab and Bitbucket an HTTPS URL works too, even the address of a page of the repository
   (`…/tree/main`): the page names the host and shows the SSH address it connects to. A URL with
   `?` or `#` is refused. **Name in Lakana** is proposed from the URL's last part, a dot
   turned into a hyphen. For an
   HTTPS URL of another host the page cannot tell its SSH address: enter the SSH URL (the
   host's Clone button shows it). **Use a token instead** shows the command that registers the
   HTTPS URL with a token, for every host; a token belongs to a person and expires, a deploy
   key belongs to the repository.
2. **Confirm the host's keys.** **Fetch the host's keys** asks a worker for them. Each key is
   its own row with its type and fingerprint; for GitHub, GitLab and Bitbucket the page links
   the host's published fingerprints. Compare each one, and tick **This matches** only for the
   keys whose fingerprint you compared. The `ssh-ed25519` key is offered first; **Show other
   key types** shows the others. Nothing is stored yet.
3. **Create the repository and its keys.** **Create the repository and its keys** registers the
   repository with the keys you ticked, and only those, then makes a key pair for every
   environment where you may manage secrets. It asks for one passkey touch for both. The page
   lists each environment's public key with **Copy key** (a key made before is marked *made
   earlier*), links the host's deploy-key page, and names the environments it could not write.
   Add each key as a read-only deploy key at your git host (below). If the repository was
   registered but its keys could not be made, the page says so with the cause; **Make the keys**
   tries only the keys again. If the repository was already registered with the same URL and
   exactly the host keys you ticked (an earlier try whose answer was lost), the page says so
   and goes on to the keys. With other host keys it stops and names `lakana repo show NAME`
   to check them, and `lakana repo host-keys URL --save known_hosts` with `lakana repo update
   NAME --known-hosts-file known_hosts` to change them; if another repository has the name,
   choose another name.
4. **Test access.** **Test access in** *environment* lets a worker read the repository with the
   key of the first environment written. It answers with the outcome and what to do about it;
   on success it shows the command to deploy and, for GitHub and GitLab, the CI how-to.

While a worker has not answered after a second, the page says what it waits for. If no worker
takes the check within two minutes, it says so and how to start one. Every check ends at most
three and a half minutes after it was asked: two minutes for a worker to take it, then at most
90 seconds to run.

The sections below take the same steps with the command line, and cover what comes later:
another environment, replacing a key.

## Confirm the host keys

A worker trusts an SSH host only with the keys you confirmed. Let a worker fetch them; you need
no `ssh-keyscan` of your own:

<!-- not tested: a worker connects to the reader's own git host -->
```bash
lakana repo host-keys git@git.example.com:acme/analytics.git --save known_hosts
```

It prints each key's type and fingerprint and, with `--save`, writes the known_hosts line of
one key: `ssh-ed25519` when the host offers it, else the strongest one offered (`--type
ssh-rsa`, repeatable, picks others; `--type` needs `--save`). `--save` never replaces an
existing file unless you add `--force`, so your own `~/.ssh/known_hosts` is safe, and it
refuses a symbolic link even with `--force`. **Compare that fingerprint with the fingerprints
your git host publishes** (GitHub, GitLab and Bitbucket each have a page for them) before you
register the repository. The keys are fetched over the network, so a fingerprint you did not
compare proves nothing.

It waits up to 60 seconds for a worker that takes pipeline work. What it may answer:

| Outcome | What to do |
|---|---|
| `refused_address` | The name resolves to an address no worker may reach (loopback, link-local, cloud metadata). Use the git host's public name. |
| `unreachable` | No address of the host offered a key (the worker tries each one, IPv6 and IPv4). Check the host and port in the URL, and that the worker's network may reach it on SSH. |
| `timeout` | The host did not answer; check the network path from the worker. |
| `no_worker` | No worker took the check within two minutes: start one (`docker compose up -d worker`, or `lakana server start --local`). |
| `failed` | The worker could not run the scan (its image lacks `ssh-keyscan`, or it refused the URL); the message says which, and the worker's log has the details (`docker compose logs worker`). |

A URL's host must be a plain name (letters, digits, dots, hyphens) or an IPv6 address in
brackets; a URL with a comma, a wildcard or an option-like part is refused.

## Register the repository and make its keys

<!-- source: docs/examples/connect-a-repository/run.sh#register -->
```bash
lakana repo add analytics --kind ssh --url git@git.example.com:acme/analytics.git \
    --known-hosts-file known_hosts
lakana repo deploy-key analytics
```

`lakana repo deploy-key` makes a key pair for every environment where you may manage secrets,
stores each private key as that environment's secret `analytics-deploy-key` (`--secret NAME`
picks another name) and makes the repository name that secret as its credential. It prints
each environment's public key on its own line, under the environment's name. An environment
that already has a key keeps it and is shown as `made earlier`, so running the command again is
safe. An environment whose secrets you may not manage gets no key: the command names it, and
someone who manages secrets there runs the same command.

## Add the keys at your git host

Add each public key at your git host as a **read-only deploy key** of the repository: in its
settings, under deploy keys (GitHub, Bitbucket) or under Settings, Repository, Deploy keys
(GitLab). **Keep write access off.** Lakana only reads, and a key that may push could change
what your production environment deploys.

Each environment has its own key, so add every one; the comment at the end of a key says which
repository it is for. `lakana repo show analytics` lists the public keys again later, for the
environments you may read.

## Test access

<!-- not tested: a worker connects to the reader's own git host -->
```bash
lakana repo test analytics --env dev
```

It prints `ok: …` once the worker could read the repository with that environment's key. Test
each environment; the outcomes and what to do about each are in
[Test access](projects.md#test-access). `auth_refused` usually means the key of that
environment is not added at the git host yet.

## An environment created later

An environment created later has no key yet: make one with `lakana repo deploy-key NAME` and
add it as an additional deploy key at your git host; the other
environments' keys keep working.

## Replace a key

<!-- source: docs/examples/connect-a-repository/run.sh#replace -->
```bash
lakana repo deploy-key analytics --environment dev --replace
```

`--replace` makes a new key only for the environments you name with `--environment`
(repeatable); without `--environment` it is refused, so you never replace every key by
accident. The command then says: "The old key for dev stops working: replace it at your git
host now." Remove the old key there and add the new one.

## Replace a leaked key

If a private key may have left your systems, stop it first: remove the deploy key at your git
host. That stops it at once, wherever it is. Then revoke the secret in each environment that
held it, and make new keys under another name (a revoked name is never used again):

<!-- source: docs/examples/connect-a-repository/run.sh#leaked -->
```bash
lakana secret revoke analytics-deploy-key --env dev --yes
lakana repo deploy-key analytics --secret analytics-deploy-key-2
```

Add the new public keys at your git host, and test each environment.

## Remove a repository

`lakana repo remove NAME --yes` removes the repository, but not its keys: each environment's
`NAME-deploy-key` secret stays stored, and its public key still reads the repository at your git
host. When you no longer need them, remove the deploy keys at your git host first, then revoke
the secret in each environment that held one, for example
`lakana secret revoke analytics-deploy-key --env dev --yes`.

A repository connected again under the same name gets new keys under another secret name: the
old keys belonged to the repository you removed, and a revoked name is never used again. The
page's **Make the keys** is refused then and names the command; run
`lakana repo deploy-key analytics --secret analytics-deploy-key-2`, then add the new public keys
at your git host.

## What is refused, and why

| Message names | Why, and what to do |
|---|---|
| a repository that is `https` or `local` | A deploy key is an SSH key. Register the SSH URL as a repository, or give the https repository a token secret ([Projects](projects.md#register-the-repository-admin)). |
| no confirmed host keys | Confirm them first (above), then `lakana repo update NAME --known-hosts-file known_hosts`. |
| a secret the repository already names | The repository has a credential you chose. Lakana does not replace it: clear it with `lakana repo update NAME --clear credential_secret` first. |
| a secret of that name in an environment | The name is taken there by a secret Lakana did not make for this repository, by a revoked one, by a key made for a repository of the same name that was removed ([above](#remove-a-repository)), or by the environment's storage or a webhook. Pick another name with `lakana repo deploy-key NAME --secret OTHER-NAME`; the page has no field for it. |
| `MANAGE_SECRET` | You may not manage secrets in any environment, or in one you named. Someone with the ADMIN role there makes the key. |
| an environment left on the old key | A new `--secret` name has to reach every environment that has a key. Someone who manages secrets in that environment makes the keys. |

A deploy key Lakana made stays with its repository: `lakana secret bind` refuses to move it to
another use, and `lakana secret rotate` refuses to change it (replace it with `--replace`
instead). `lakana secret revoke` works as for any secret.
