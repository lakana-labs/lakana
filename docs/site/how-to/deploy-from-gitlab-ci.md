# Deploy from GitLab CI without a stored secret

A GitLab CI pipeline can deploy a Lakana project without a Lakana token stored in GitLab. Each
job declares an OIDC ID token, a signed statement of which project, branch and commit the job runs
for, and exchanges it with the Lakana server for a short-lived CI token. The CI token may queue
one deploy, of the job's own commit, or one dry run, and nothing else. The files `lakana init`
writes work this way.

At the end of this page, merge requests are dry-run against `dev` and every pipeline on `main` is
deployed to `prod`, with no secret in the project's CI/CD settings.

## Before you start

- The operator has turned the exchange on: the server's `LAKANA_CI_OIDC__ISSUERS` includes
  `gitlab` (GitLab.com; a self-hosted GitLab is named by its URL), and
  `LAKANA_CI_OIDC__AUDIENCE` is the server's URL, exactly as the pipeline's `LAKANA_URL` will name
  it ([Settings](../reference/settings.md)).
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
which environment and repository. Put your project's id, the number on the project's overview
page, in place of `123456`.

<!-- source: docs/examples/ci-trust-rules.sh#gitlab -->
```bash
lakana ci-trust create --issuer gitlab --principal ci-dev --env dev --repo analytics \
  --kind dry_run --claim project_id=123456 --claim environment=lakana-dry-run
lakana ci-trust create --issuer gitlab --principal ci-prod --env prod --repo analytics \
  --claim project_id=123456 --claim ref=main --claim ref_type=branch \
  --claim ref_protected=true --claim environment=prod
```

Why each condition is there:

- **`project_id`** pins the project by its number. A path can be taken over: rename or transfer
  the project, and someone else can create one at the old path. The number stays.
- **`ref=main`** and **`ref_type=branch`** mean only the `main` branch deploys to `prod`. GitLab
  writes a tag's name in `ref` too: without `ref_type`, a protected tag named `main` would match.
- **`ref_protected=true`** pins how the run was started, which the server requires. A `ref`
  condition alone also matches `main` while it is not protected, and pipelines of every source.
  With it, only pipelines of the protected branch match, which only people allowed to merge into
  it can start. Add `--claim pipeline_source=push` as well to leave out schedules, triggers and
  pipelines started from the web interface or the API.
- **`environment=prod`** binds the job to the GitLab environment `prod`. Protect it under
  *Settings > CI/CD > Protected environments* to require an approval for each deploy.
- The `dev` rule allows only `dry_run`, for jobs in the environment `lakana-dry-run`. Any branch
  of the project can therefore dry-run against `dev`, and none can deploy with it.

The full list of rules and conditions is in [CI trust rules and CI tokens](../reference/ci-trust-rules.md).

## 3. Add the pipeline

`lakana init` writes this file as `.gitlab-ci.yml`, with your repository's name and your CLI's
version in place of `<version>`:

<!-- source: docs/examples/ci-oidc-gitlab-ci.yml -->
```yaml
# Deploys main to prod; dry-runs merge requests against dev. No Lakana token is stored in GitLab:
# each job declares an OIDC ID token (`id_tokens:`) and exchanges it with the Lakana server for a
# CI token that may queue one dry run, or one deploy of the job's own commit.
# lakana is pinned to the CLI version that generated this file and installed from PyPI. To use a
# mirror or a build you host yourself, set the CI/CD variable LAKANA_INDEX_URL to that index
# (see https://lakana-labs.github.io/lakana/how-to/projects/).
# CI/CD variable (not a secret): LAKANA_URL, the server's URL. The server's
# LAKANA_CI_OIDC__AUDIENCE must be the same text (the `aud:` lines below use it).
# Protect main under Settings > Repository. Never add a variable named LAKANA_TOKEN: next to the
# ID token, lakana refuses to guess which credential you meant.
# Run once, as an instance administrator. ci-dev holds DEPLOY on dev and ci-prod on prod; put
# your project's id in place of 123456 (the number on the project's overview page):
#   lakana ci-trust create --issuer gitlab --principal ci-dev --env dev --repo analytics \
#     --kind dry_run --claim project_id=123456 --claim environment=lakana-dry-run
#   lakana ci-trust create --issuer gitlab --principal ci-prod --env prod --repo analytics \
#     --claim project_id=123456 --claim ref=main --claim ref_type=branch \
#     --claim ref_protected=true --claim environment=prod
# Each ID token is exchanged once, so each job submits once.
# How-to: https://lakana-labs.github.io/lakana/how-to/deploy-from-gitlab-ci/
stages: [deploy]

lakana-dry-run:
  stage: deploy
  image: python:3.14-slim
  environment: lakana-dry-run
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  id_tokens:
    LAKANA_OIDC_TOKEN:
      aud: $LAKANA_URL
  variables:
    LAKANA_OIDC: gitlab
  script:
    - pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"
    - lakana deploy --env dev --ref "$CI_COMMIT_SHA" --dry-run --timeout 1800

lakana-deploy:
  stage: deploy
  image: python:3.14-slim
  environment: prod
  resource_group: lakana-deploy-prod
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  id_tokens:
    LAKANA_OIDC_TOKEN:
      aud: $LAKANA_URL
  variables:
    LAKANA_OIDC: gitlab
  script:
    - pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"
    - lakana deploy --env prod --ref "$CI_COMMIT_SHA" --timeout 1800
```

Then, in the project's settings on GitLab:

1. Under *Settings > CI/CD > Variables*, add `LAKANA_URL` with the server's URL. It is not a
   secret; do not mask or protect it. Never add a variable named `LAKANA_TOKEN`: next to the ID
   token, `lakana deploy` refuses to guess which credential you meant.
2. Under *Settings > Repository > Protected branches*, protect `main`.

Nothing else: no secret, no token to rotate.

## What happens in a run

The `id_tokens:` entry makes GitLab put an ID token for the audience `$LAKANA_URL` into the
variable `LAKANA_OIDC_TOKEN`, and `LAKANA_OIDC: gitlab` tells `lakana deploy` to exchange it
instead of reading a stored token. Before it deploys, `lakana deploy` says what the CI token may
do, never the token:

<!-- output -->
```text
Exchanged the CI job's ID token: acting as ci-prod for one deploy of analytics at 0123456789abcdef0123456789abcdef01234567 to prod, until 2026-10-20T23:34:04.293048+00:00.
```

- On a merge request, `lakana-dry-run` dry-runs the commit against `dev`.
- On `main`, `lakana-deploy` deploys that commit to `prod`. The CI token may deploy that commit
  and no other: leave out `--ref`, or pass `$CI_COMMIT_SHA`, never a branch.

### One submission per ID token

GitLab gives a job each ID token it declares once, and the server exchanges each ID token once, so
a job submits once per ID token. A second `lakana deploy` in the same job is refused, with a
message that says so. To run a dry run and then a deploy in one job, declare a second ID token
and give it to the second command; or run each in a job of its own, as the file above does. The
job runs on `main` in the environment `prod`, like `lakana-deploy`, so the prod rule matches its
ID tokens:

<!-- source: docs/examples/ci-oidc-gitlab-one-job.yml -->
```yaml
lakana-dry-run-then-deploy:
  stage: deploy
  image: python:3.14-slim
  environment: prod
  resource_group: lakana-deploy-prod
  rules:
    - if: $CI_COMMIT_BRANCH == "main"
  id_tokens:
    LAKANA_OIDC_TOKEN:
      aud: $LAKANA_URL
    LAKANA_OIDC_TOKEN_2:
      aud: $LAKANA_URL
  variables:
    LAKANA_OIDC: gitlab
  script:
    - pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==<version>"
    - lakana deploy --env prod --ref "$CI_COMMIT_SHA" --dry-run --timeout 1800
    - LAKANA_OIDC_TOKEN="$LAKANA_OIDC_TOKEN_2" lakana deploy --env prod --ref "$CI_COMMIT_SHA" --timeout 1800
```

The prod rule must then allow both kinds; give it the dry run as well (`lakana ci-trust list`
shows its id):

<!-- source: docs/examples/ci-trust-rules.sh#both-kinds -->
```bash
lakana ci-trust update RULE_ID --kind deploy --kind dry_run
```

## Start the project's jobs after a deploy

A job with `on_deploy: true` in `lakana.yaml` starts after each deploy
([Start a job after each deploy](run-a-job-on-a-schedule.md#start-a-job-after-each-deploy)). A
deploy through a CI token starts it only when the trust rule opts in; without that, the deploy
succeeds and its warning names each job it did not start. To opt in, for the prod rule
(`lakana ci-trust list` shows its id):

<!-- source: docs/examples/ci-trust-rules.sh#start-jobs -->
```bash
lakana ci-trust update RULE_ID --start-on-deploy-jobs
```

The deploys under that rule then start the deployed project's `on_deploy` jobs in that
environment, as the rule's account, and nothing else: the CI token still cannot start any other
run. In a protected environment the run writes as the bound run principal and needs one. The
audit event of each run names the rule ([CI trust rules](../reference/ci-trust-rules.md)).

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
| LAKANA_OIDC=gitlab reads the job's ID token from LAKANA_OIDC_TOKEN, which is not set | The job has no `id_tokens:` entry named `LAKANA_OIDC_TOKEN`. | Add it, as in the file above. |
| Both LAKANA_TOKEN and LAKANA_OIDC are set | A stored token is still set next to `LAKANA_OIDC`. | Remove the variable `LAKANA_TOKEN` from the job and the project. |
| `disabled` | The exchange is off on the server. | Ask the operator to set `LAKANA_CI_OIDC__ISSUERS`. |
| `issuer_not_allowed` | The server does not trust this GitLab. | Ask the operator to add `gitlab`, or your GitLab's URL, to `LAKANA_CI_OIDC__ISSUERS`. |
| `audience_mismatch` | The `aud:` of the ID token is not the server's audience. | Make `LAKANA_URL` the same text as `LAKANA_CI_OIDC__AUDIENCE`. |
| `expired`, `too_old` | The ID token is too old by the time it reaches the server: a long job exchanges late. | Run the job again; deploy early in the job. |
| `replayed` | This ID token was exchanged before. | One submission per ID token; see above. |
| `no_matching_rule` | No enabled rule matches the claims, environment and repository. | Compare the claims with the rules with `explain`, below. |
| `ambiguous_rules` | Two rules match. | Disable or narrow one of them. |
| `principal_inactive` | The rule's account was deactivated. | Point the rule at an active account (`lakana ci-trust update`). |
| `commit_missing` | A rule that allows `deploy` met an ID token without a commit. | Deploy from a branch pipeline, which has one. |
| only `lakana deploy` exchanges the CI job's ID token | Another command ran with `LAKANA_OIDC` set. | A CI token may only deploy; run other commands with a stored token. |

`explain` compares an ID token's claims with every rule, without any token. Write the claims of
the job as a file `claims.json`; every one of them is a predefined CI/CD variable
(`CI_PROJECT_ID`, `CI_COMMIT_REF_NAME`, `CI_COMMIT_REF_PROTECTED`, `CI_PIPELINE_SOURCE`,
`CI_ENVIRONMENT_NAME`, `CI_COMMIT_SHA`; `ref_type` is `tag` when `CI_COMMIT_TAG` is set, and
`branch` otherwise):

<!-- source: docs/examples/ci-trust-claims-gitlab.json -->
```json
{
  "iss": "https://gitlab.com",
  "project_id": "123456",
  "ref": "main",
  "ref_type": "branch",
  "ref_protected": "true",
  "pipeline_source": "push",
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
Verdict: accepted by rule 3fb34c95-1bb1-4979-968e-99aa864322c4
  3d6210f0-d778-47e3-bcd4-26a1395ca094: does not match
    - the rule is for environment 'dev', not 'prod'
    - claim environment is 'prod'; the rule needs 'lakana-dry-run'
  3fb34c95-1bb1-4979-968e-99aa864322c4: matches
```

A rule that does not match lists each claim it disagrees with.

## From Python

The SDK does the same exchange, reading `LAKANA_OIDC_TOKEN`; build one client per submission:

```python
import lakana

client = lakana.Client.from_ci_oidc(
    "https://lakana.example.com", environment="prod", repository="analytics", provider="gitlab"
)
print(client.whoami().ci_token)
client.deploys.create("prod", "analytics", kind="deploy")
```

`client.whoami().ci_token` shows the rule, the environment, the repository, the allowed kinds,
the commit, the expiry and whether the token was used; never the token.

## Without OIDC

`lakana init --ci-auth pat` writes CI files that read a stored Lakana token instead; existing
pipelines that use a stored token keep working. [Projects and deploys](projects.md#ci-deploys-to-protected-environments)
describes that setup.
