# Deploy from GitHub Actions without a stored secret

A GitHub Actions workflow can deploy a Lakana project without a Lakana token stored in GitHub.
Each job asks GitHub for an OIDC ID token, a signed statement of which repository, branch, event
and commit the job runs for, and exchanges it with the Lakana server for a short-lived CI token.
The CI token may queue one deploy, of the job's own commit, or one dry run, and nothing else. The
files `lakana init` writes work this way. Such a deploy starts no job with `on_deploy: true`
([CI trust rules](../reference/ci-trust-rules.md)).

At the end of this page, pull requests are dry-run against `dev` and every push to `main` is
deployed to `prod`, with no secret in the repository's settings.

## Before you start

- The operator has turned the exchange on: the server's `LAKANA_CI_OIDC__ISSUERS` includes
  `github`, and `LAKANA_CI_OIDC__AUDIENCE` is the server's URL, exactly as the workflow's
  `LAKANA_URL` will name it ([Settings](../reference/settings.md)).
- The project's repository is registered in Lakana as `analytics` and the environments `dev` and
  `prod` exist ([Projects and deploys](projects.md)).
- You are an administrator of the instance: only an administrator creates trust rules.

## 1. Create the accounts the jobs act as

A CI job acts as a service account, which needs its own `DEPLOY` grant, and `VIEWER` to read the
tables the project reads. Use two accounts, one per environment:

<!-- source: docs/examples/ci-trust-rules.sh#accounts -->
```bash
lakana principal create ci-dev --type service-account
lakana principal create ci-prod --type service-account
lakana grant create --principal ci-dev --action DEPLOY --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --action DEPLOY --kind ENVIRONMENT --env prod
lakana grant create --principal ci-dev --role VIEWER --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --role VIEWER --kind ENVIRONMENT --env prod
```

No token is issued for them: the trust rules below are how CI signs in.

## 2. Create the trust rules

A trust rule says which jobs, by the claims of their ID token, may act as which account for
which environment and repository. Put your repository's numeric id in place of `123456`; the
comment at the top of the workflow file shows a `gh` command that prints it.

<!-- source: docs/examples/ci-trust-rules.sh#github -->
```bash
lakana ci-trust create --issuer github --principal ci-dev --env dev --repo analytics \
  --kind dry_run --claim repository_id=123456 --claim environment=lakana-dry-run
lakana ci-trust create --issuer github --principal ci-prod --env prod --repo analytics \
  --claim repository_id=123456 --claim ref=refs/heads/main \
  --claim event_name=push --claim environment=prod
```

Why each condition is there:

- **`repository_id`** pins the repository by its number. A name can be taken over: rename or
  transfer the repository, and someone else can create one with the old name. The number stays.
- **`ref=refs/heads/main`** means only the `main` branch deploys to `prod`.
- **`event_name=push`** pins how the run was started, which the server requires. A ref alone does
  not say it: a `pull_request_target` run, which a pull request from a fork can start, gets an ID
  token whose `ref` is the base branch, `refs/heads/main`.
- **`environment=prod`** binds the job to the GitHub environment `prod`, whose protection rules
  (the `main` branch only, required reviewers) GitHub enforces before the job gets its ID token.
- The `dev` rule allows only `dry_run`, for jobs in the GitHub environment `lakana-dry-run`. Any
  branch of the repository can therefore dry-run against `dev`, and none can deploy with it.

The full list of rules and conditions is in [CI trust rules and CI tokens](../reference/ci-trust-rules.md).

## 3. Add the workflow

`lakana init` writes this file as `.github/workflows/lakana-deploy.yml`, with your repository's
name and your CLI's version in place of `<version>`:

<!-- source: docs/examples/ci-oidc-github-actions.yml -->
```yaml
# Deploys main to prod; dry-runs pull requests against dev. No Lakana token is stored in GitHub:
# each job asks GitHub for an OIDC ID token (`id-token: write`) and exchanges it with the Lakana
# server for a CI token that may queue one dry run, or one deploy of the job's own commit.
# lakana is pinned to the CLI version that generated this file and installed from PyPI. To use a
# mirror or a build you host yourself, set the repository variable LAKANA_INDEX_URL to that
# index (see https://lakana-labs.github.io/lakana/how-to/projects/).
# Actions are pinned to full commit SHAs; bump them deliberately (Dependabot understands this form).
# Repository variable (not a secret): LAKANA_URL, the server's URL. The server's
# LAKANA_CI_OIDC__AUDIENCE must be the same text (or set LAKANA_OIDC_AUDIENCE in both jobs).
# GitHub environments: "lakana-dry-run" (no protection rules needed) and "prod" (restrict it to
# the main branch, and add required reviewers if you want them).
# Run once, as an instance administrator. ci-dev holds DEPLOY on dev and ci-prod on prod; put
# your repository's id in place of 123456 (`gh api repos/OWNER/REPO --jq .id` prints it):
#   lakana ci-trust create --issuer github --principal ci-dev --env dev --repo analytics \
#     --kind dry_run --claim repository_id=123456 --claim environment=lakana-dry-run
#   lakana ci-trust create --issuer github --principal ci-prod --env prod --repo analytics \
#     --claim repository_id=123456 --claim ref=refs/heads/main \
#     --claim event_name=push --claim environment=prod
# Fork pull requests get no ID token; their dry run is skipped.
# How-to: https://lakana-labs.github.io/lakana/how-to/deploy-from-github-actions/
name: lakana deploy
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  dry-run:
    if: >-
      github.event_name == 'pull_request' &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    environment: lakana-dry-run
    permissions:
      contents: read
      id-token: write
    concurrency:
      group: lakana-deploy-${{ github.ref }}
      cancel-in-progress: true
    env:
      LAKANA_URL: ${{ vars.LAKANA_URL }}
      LAKANA_OIDC: github
      LAKANA_INDEX_URL: ${{ vars.LAKANA_INDEX_URL }}
      REF_SHA: ${{ github.event.pull_request.head.sha }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.14"
      - name: Install lakana
        run: |
          pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"
      - name: Dry run against dev
        run: lakana deploy --env dev --ref "$REF_SHA" --dry-run --timeout 1800
  deploy:
    if: github.event_name == 'push'
    runs-on: ubuntu-latest
    environment: prod
    permissions:
      contents: read
      id-token: write
    concurrency:
      group: lakana-deploy-prod
      cancel-in-progress: false
    env:
      LAKANA_URL: ${{ vars.LAKANA_URL }}
      LAKANA_OIDC: github
      LAKANA_INDEX_URL: ${{ vars.LAKANA_INDEX_URL }}
      REF_SHA: ${{ github.sha }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.14"
      - name: Install lakana
        run: |
          pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"
      - name: Deploy to prod
        run: lakana deploy --env prod --ref "$REF_SHA" --timeout 1800
```

Then, in the repository's settings on GitHub:

1. Under *Secrets and variables > Actions > Variables*, add the repository variable `LAKANA_URL`
   with the server's URL. It is not a secret.
2. Under *Environments*, create `lakana-dry-run` (no rules needed) and `prod`. Restrict `prod` to
   the `main` branch, and add required reviewers if a person should approve each deploy.

Nothing else: no secret, no token to rotate.

## What happens in a run

`LAKANA_OIDC: github` tells `lakana deploy` to ask the runner for an ID token, for the audience
`LAKANA_URL`, and to exchange it instead of reading a stored token. The job needs
`permissions: id-token: write` for that; the workflow gives it to these two jobs only. Before it
deploys, `lakana deploy` says what the CI token may do, never the token:

<!-- output -->
```text
Exchanged the CI job's ID token: acting as ci-prod for one deploy of analytics at 0123456789abcdef0123456789abcdef01234567 to prod, until 2026-10-20T23:34:04.293048+00:00.
```

- On a pull request, the `dry-run` job dry-runs the pull request's commit against `dev`. A pull
  request from a fork gets no ID token from GitHub; the job is skipped.
- On a push to `main`, the `deploy` job deploys that commit to `prod`. The CI token may deploy
  that commit and no other: leave out `--ref`, or pass the commit, never a branch.
- Each `lakana deploy` makes its own exchange. A job that runs a dry run and then a deploy gets a
  new ID token from GitHub for each, without anything to configure.

## What a stolen CI token can do

Very little, by design. A CI token expires after an hour (the operator's
`LAKANA_CI_OIDC__TOKEN_TTL_SECONDS`), may queue one deploy of the one commit the job ran for (or
one dry run), to one environment, of one repository, and then only read that run. It cannot roll
back, undeploy, read tables or run queries, whatever its account's grants say. An ID token is
exchanged once: a copy taken from a job's log cannot be exchanged again. Disabling the rule ends
every CI token issued under it at once (`lakana ci-trust list` shows the rules' ids):

<!-- source: docs/examples/ci-trust-rules.sh#disable -->
```bash
lakana ci-trust disable RULE_ID
```

## Troubleshooting

A refused exchange is recorded in the audit log as `OIDC_EXCHANGE_REFUSED`, with a `code`. The
server's answer to the job names the cause too, except for the 403, which says the same for every
reason so that it does not tell what exists.

| Message or code | Cause | What to do |
|---|---|---|
| This job cannot ask GitHub Actions for an ID token | The job has no `permissions: id-token: write`, or it runs for a pull request from a fork. | Add the permission to the job. |
| Both LAKANA_TOKEN and LAKANA_OIDC are set | A stored token is still set next to `LAKANA_OIDC`. | Remove `LAKANA_TOKEN` from the job and the repository's secrets. |
| `disabled` | The exchange is off on the server. | Ask the operator to set `LAKANA_CI_OIDC__ISSUERS`. |
| `issuer_not_allowed` | The server does not trust GitHub. | Ask the operator to add `github` to `LAKANA_CI_OIDC__ISSUERS`. |
| `audience_mismatch` | The ID token was issued for another audience than the server's. | Make `LAKANA_URL` the same text as `LAKANA_CI_OIDC__AUDIENCE`, or set `LAKANA_OIDC_AUDIENCE` in the job. |
| `expired`, `too_old` | The ID token is too old by the time it reaches the server. | Run the job again; check the runner's clock. |
| `replayed` | This ID token was exchanged before. | Run the job again; each run asks for new ones. |
| `no_matching_rule` | No enabled rule matches the claims, environment and repository. | Compare the claims with the rules with `explain`, below. |
| `ambiguous_rules` | Two rules match. | Disable or narrow one of them. |
| `principal_inactive` | The rule's account was deactivated. | Point the rule at an active account (`lakana ci-trust update`). |
| `commit_missing` | A rule that allows `deploy` met an ID token without a commit. | Deploy from a `push` run, which has one. |
| only `lakana deploy` exchanges the CI job's ID token | Another command ran with `LAKANA_OIDC` set. | A CI token may only deploy; run other commands with a stored token. |

`explain` compares an ID token's claims with every rule, without any token. Write the claims of
the job as a file `claims.json`; every one of them is in the job's GitHub context (`github.ref`,
`github.event_name`, the environment's name):

<!-- source: docs/examples/ci-trust-claims-github.json -->
```json
{
  "iss": "https://token.actions.githubusercontent.com",
  "repository_id": "123456",
  "ref": "refs/heads/main",
  "event_name": "push",
  "environment": "prod",
  "sha": "0123456789abcdef0123456789abcdef01234567"
}
```

<!-- source: docs/examples/ci-trust-rules.sh#explain -->
```bash
lakana ci-trust explain --claims claims.json --env prod --repo analytics
```

<!-- output -->
```text
Verdict: accepted by rule 00ea0aa7-8442-4e35-8b22-f7f524dc83eb
  441e3a9f-7909-4002-a301-dfdead68cd8d: does not match
    - the rule is for environment 'dev', not 'prod'
    - claim environment is 'prod'; the rule needs 'lakana-dry-run'
  00ea0aa7-8442-4e35-8b22-f7f524dc83eb: matches
```

A rule that does not match lists each claim it disagrees with.

## From Python

The SDK does the same exchange; build one client per submission:

```python
import lakana

client = lakana.Client.from_ci_oidc(
    "https://lakana.example.com", environment="prod", repository="analytics", provider="github"
)
print(client.whoami().ci_token)
client.deploys.create("prod", "analytics", kind="deploy")
```

`client.whoami().ci_token` shows the rule, the environment, the repository, the allowed kinds,
the commit, the expiry and whether the token was used; never the token.

## Without OIDC

`lakana init --ci-auth pat` writes CI files that read a stored Lakana token instead; existing
workflows that use a stored token keep working. [Projects and deploys](projects.md#ci-deploys-to-protected-environments)
describes that setup.
