# CI trust rules and CI tokens

A CI job on GitHub Actions or GitLab CI can deploy without a stored Lakana token. The job asks its
CI provider for an OIDC ID token, a signed statement of which repository, branch and commit the job
runs for, and exchanges it with the Lakana server for a short-lived **CI token**. The exchange
succeeds only when exactly one enabled **CI trust rule** matches the ID token. The CI token acts as
the rule's service account and may queue one deploy, of the commit the job runs at, or one dry
run, to one environment, and then follow that run. Nothing secret is stored in CI.

A CI token may not start a run. Only when its rule opts in (`start_on_deploy_jobs`, below) does a
deploy it queued start the project's jobs with `on_deploy: true`, in the rule's environment, and
nothing else. Without the opt-in the deploy succeeds and names each job it did not start
([Start a job after each deploy](../how-to/run-a-job-on-a-schedule.md#start-a-job-after-each-deploy)).

This page lists the exact rules. The operator turns the exchange on with the `LAKANA_CI_OIDC__*`
settings ([Settings](settings.md)); an administrator manages the rules with `lakana ci-trust`
([Command line](cli.md)). The how-tos [Deploy from GitHub Actions without a stored
secret](../how-to/deploy-from-github-actions.md) and [Deploy from GitLab CI without a stored
secret](../how-to/deploy-from-gitlab-ci.md) set it up from start to end.

## The rule

| Field | Type | Meaning |
|---|---|---|
| `id` | UUID | The rule's identifier. |
| `issuer` | text | The CI provider: GitHub Actions (`https://token.actions.githubusercontent.com`), GitLab.com (`https://gitlab.com`) or a self-hosted GitLab. Given as `github`, `gitlab` or the issuer's URL; it must be one of `LAKANA_CI_OIDC__ISSUERS`. Never changes. |
| `principal` | text | The service account the CI job acts as. It must be active, and it still needs its own `DEPLOY` grant on the environment. |
| `environment` | text | The one environment the CI job may deploy to. Never changes. |
| `repository` | text | The one registered Lakana repository it may deploy. Never changes. |
| `kinds` | list | `deploy`, `dry_run` or both; `deploy` by default. A rollback and an undeploy are never allowed through CI. |
| `conditions` | object | Claim name to the exact text the ID token's claim must have. |
| `enabled` | true or false | A disabled rule matches no ID token. |
| `start_on_deploy_jobs` | true or false | `false` by default. With `true`, a deploy accepted under the rule starts the deployed project's jobs with `on_deploy: true` in the rule's environment, as the rule's service account, allowed by this setting and not by the account's `RUN` grant. It allows no other run, and the rules of a protected environment still apply: the run writes as the bound run principal, and without one it is refused. The run's `RUN_STARTED` event names the rule (`ci_rule_id`) and `allowed_by: start_on_deploy_jobs`. Set with `lakana ci-trust create --start-on-deploy-jobs` or `lakana ci-trust update RULE_ID --start-on-deploy-jobs` (`--no-start-on-deploy-jobs` turns it off); changing it is a rule change like any other, and only an instance administrator may. The rule is read again when each job starts, so a rule changed or disabled while a deploy runs starts nothing, and a job starts only if it still has `on_deploy: true` in the project version it would run. |
| `created_by`, `updated_by` | text or `null` | Who created it, who changed it last. |
| `created_at`, `updated_at` | time | When. |

A rule is bound to the ids of its environment, repository and service account, not their names:
a renamed or re-created object does not inherit it. Deleting the environment, the repository or
the service account deletes the rule.

### Conditions

- Every condition must match **exactly**: the claim is present, it is a text, and it has the same
  characters. There is no case folding, no trimming, no pattern, and a number never matches a text.
- A rule must pin the provider's **numeric repository id**: `repository_id` for GitHub,
  `project_id` for GitLab, as digits (GitHub and GitLab write it as a text, such as `"123456"`).
  A rename or a transfer of the repository cannot take it over, unlike its name.
- A rule must also pin `ref` or `environment`, so that not every branch may deploy. On GitHub
  `ref` is `refs/heads/main`; on GitLab it is `main`. `environment` is the CI environment the job
  runs in (a GitHub environment, a GitLab environment).
- A rule must also pin **how the run was started**, because a ref alone does not say who started
  it:
  - **GitHub:** `event_name` (for example `push`) or `environment`. A `pull_request_target` run,
    which a pull request from a fork starts, gets an ID token whose `ref` is the base branch.
  - **GitLab:** `ref_protected` (`true`), `pipeline_source` (for example `push`) or
    `environment`. A `ref` condition alone also matches the branch while it is not protected,
    and pipelines of every source: schedules, triggers, the web interface and the API.

  An `environment` condition counts for both, since the environment's protection rules on GitHub
  or GitLab decide which runs may use it. A rule without one is refused with a message that
  names the condition to add, such as `--claim event_name=push` or
  `--claim environment=production`.
- At most 16 conditions. A claim name is lower-case letters, digits and underscores, starting with
  a letter, at most 64 characters. A value is 1 to 255 characters without a control character.
- `iss`, `aud`, `exp`, `iat`, `nbf` and `jti` cannot be conditions: the server checks them for every
  ID token, or they differ for each one.

A rule stored before this requirement (only an unreleased build could write one) matches no ID
token, cannot be enabled or changed until its conditions are fixed, and `explain` says so.

### Changing a rule

The principal, the kinds and the conditions can change; the issuer, the environment and the
repository cannot (create another rule). Every change, and every disable or enable, ends the CI
tokens issued under the rule before it at once. Enabling a rule again does not bring them back.

## The exchange

`POST /api/v1/auth/oidc/token`, with no `Authorization` header, a JSON body of at most 16 KiB:

```json
{
  "id_token": "<the job's ID token>",
  "environment": "prod",
  "repository": "analytics"
}
```

The server, in this order:

1. verifies the ID token: its issuer is one of `LAKANA_CI_OIDC__ISSUERS`, its signature checks
   with a key the issuer publishes (the server fetches them over HTTPS, see
   [What leaves the machine](../explanation/what-leaves-the-machine.md)), its audience is
   `LAKANA_CI_OIDC__AUDIENCE`, it has not expired and it is at most
   `LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS` old;
2. records the ID token's `jti`: **each ID token is exchanged once**, even when the exchange is
   then refused. Request a new ID token for every exchange;
3. finds the enabled rules of the token's issuer for the environment and repository asked for and
   compares their conditions with the claims; none, or more than one, refuses the exchange;
4. requires the rule's service account to be active, and, for a rule that allows `deploy`, a
   `sha` claim with the full commit id (40 lower-case hexadecimal digits);
5. issues the CI token.

The answer:

```json
{
  "token": "lakana_ci_<key id>_<secret>",
  "expires_at": "2026-10-20T13:00:00Z",
  "principal": "ci-prod",
  "environment": "prod",
  "repository": "analytics",
  "kinds": ["deploy"],
  "sha": "0123456789abcdef0123456789abcdef01234567",
  "rule_id": "0b6f1d9e-3c2a-4e5b-8f7d-1a2b3c4d5e6f"
}
```

The token is shown once and stored only as a hash. It expires after
`LAKANA_CI_OIDC__TOKEN_TTL_SECONDS` (3600, one hour, by default; at most 21600, six hours).

### Refusals

| Status | When | What the caller is told |
|---|---|---|
| 401 | The ID token is refused: malformed, an issuer that is not configured, a bad signature, another audience, expired, too old, and the like. | The reason, which names the setting to check. |
| 401 | CI OIDC is off (`LAKANA_CI_OIDC__ISSUERS` is empty). | That, and the setting to set. |
| 401 | The ID token was exchanged before. | To request a new ID token. |
| 403 | No enabled rule matches, two match, the environment or repository does not exist, the service account is inactive, or a rule that allows `deploy` meets an ID token without a commit. | One text for all of them, naming the environment and repository asked for. |
| 413 | The body is over 16 KiB. | The limit. |
| 422 | The body is not a JSON object with exactly the three texts. | Which fields are wrong, never their values. |

The 403 is the same whatever the reason, so the answer does not tell whether a rule, an
environment or a repository exists. The reason is in the audit log (below), and `lakana ci-trust
explain` compares a CI job's claims with every rule without any token.

## What a CI token may do

| Request | With a CI token |
|---|---|
| Queue a deploy of its repository in its environment | Once, and only of its `sha`: leave out the ref, or pass that commit. Another commit, a branch or a tag is refused. The worker checks again that the fetched commit is that one. |
| Queue a dry run | Once (instead of a deploy), of any branch, tag or commit of its repository, if the rule allows `dry_run`. |
| Read a deploy run | Only the one it queued. Any other run answers 404, as one that does not exist. |
| Roll back, undeploy, pause, resume or test a job, read a notification delivery | Refused. |
| Anything else: read the environment, its deploy history, tables, runs or queries; manage anything | Refused, whatever the service account's own grants allow. |
| `GET /api/v1/whoami` | Answers with the service account, and in `ci_token` what the token may do: `rule_id`, `environment`, `repository`, `kinds`, `sha`, `expires_at` and `used` (whether it has queued its run). Never the token. |

A CI token never widens what its service account may do: the account needs `DEPLOY` on the
environment, and the deploy itself checks the tables with the account's grants, as any deploy
does. Each refusal is recorded as `AUTHORIZATION_DENIED` with the reason `ci_token`.

The token stops working at the next request when it expires, when its rule is disabled, changed or
deleted, when its service account is deactivated, or when its environment is deleted. A deploy it
queued before that is refused by the worker when the rule is disabled, changed or deleted or the
account is deactivated; it is not refused because the token expired while the deploy waited.

A stolen CI token can therefore queue one deploy of one commit that CI signed, to one environment,
within its lifetime, and nothing else.

## Commands, SDK and routes

| Command | SDK | Route |
|---|---|---|
| `lakana ci-trust create` | `client.ci_trust_rules.create` | `POST /api/v1/ci-trust-rules` |
| `lakana ci-trust list` | `client.ci_trust_rules.list` | `GET /api/v1/ci-trust-rules` |
| `lakana ci-trust show` | `client.ci_trust_rules.get` | `GET /api/v1/ci-trust-rules/{rule_id}` |
| `lakana ci-trust update` | `client.ci_trust_rules.update` | `PATCH /api/v1/ci-trust-rules/{rule_id}` |
| `lakana ci-trust enable` | `client.ci_trust_rules.enable` | `POST /api/v1/ci-trust-rules/{rule_id}/enable` |
| `lakana ci-trust disable` | `client.ci_trust_rules.disable` | `POST /api/v1/ci-trust-rules/{rule_id}/disable` |
| `lakana ci-trust delete` | `client.ci_trust_rules.delete` | `DELETE /api/v1/ci-trust-rules/{rule_id}` |
| `lakana ci-trust explain` | `client.ci_trust_rules.explain` | `POST /api/v1/ci-trust-rules/explain` |
| `lakana deploy` with `LAKANA_OIDC` or `LAKANA_OIDC_TOKEN` (below) | `lakana.Client.from_ci_oidc`, or `lakana.resources.ci_trust_rules.exchange_id_token` | `POST /api/v1/auth/oidc/token` |

Every rule route needs the `ADMIN` role on the instance: a rule lets a CI job act as a service
account, which is the same as issuing that account a token. `explain` takes the decoded claims of
an ID token (the middle part of the token, as JSON, never the token itself), the environment and
the repository:

```json
{
  "claims": {
    "iss": "https://token.actions.githubusercontent.com",
    "repository_id": "123456",
    "ref": "refs/heads/main",
    "sha": "0123456789abcdef0123456789abcdef01234567"
  },
  "environment": "prod",
  "repository": "analytics"
}
```

It answers with a verdict (`accepted by rule …` or `refused: …`), the ids of the matching rules,
and for every rule the reasons it does not match. It records nothing and issues nothing.

## In the CI job

`lakana deploy` (with or without `--dry-run`) exchanges the job's ID token instead of reading a
stored token when one of these is set:

| Variable | Meaning |
|---|---|
| `LAKANA_OIDC` | `github`: ask the GitHub Actions runner for a new ID token (the job needs `permissions: id-token: write`). `gitlab`: read it from `LAKANA_OIDC_TOKEN`, which the job's `id_tokens:` entry of that name sets. Any other value is refused. |
| `LAKANA_OIDC_TOKEN` | The ID token itself. Without `LAKANA_OIDC`, it is exchanged as it is. |
| `LAKANA_OIDC_AUDIENCE` | The audience to ask GitHub for; `LAKANA_URL` by default. It must equal `LAKANA_CI_OIDC__AUDIENCE`. |
| `LAKANA_URL` | The server, as always. It is required: there is no profile in a CI job. |

- `LAKANA_TOKEN` set as well is refused (exit 3): which credential is meant is not guessed.
- Any other command refuses to run with `LAKANA_OIDC` set, since a CI token may do nothing else.
- Each `lakana deploy` makes one exchange, so a dry run followed by a deploy makes two. On GitHub
  each gets a new ID token from the runner. On GitLab a job gets each ID token it declares once:
  declare a second one for a second submission, or use a second job.
- Before it submits, `lakana deploy` prints on standard error the account, the kinds, the
  repository, the commit, the environment and the expiry of the CI token, never the token.
- The ID token and the CI token stay in the process's memory: they are never written to a file,
  a log, an error message or a `repr`.

`Client.from_ci_oidc(url, *, environment, repository, provider=None, id_token=None,
audience=None)` does the same in Python and returns a client holding the CI token; `id_token`
wins over `provider`, and `provider=None` reads `LAKANA_OIDC`. Its `ci_token` attribute describes
the token without showing it.

## Audit events

| Event | When | Detail |
|---|---|---|
| `CI_TRUST_RULE_CREATED`, `CI_TRUST_RULE_UPDATED`, `CI_TRUST_RULE_DELETED`, `CI_TRUST_RULE_ENABLED`, `CI_TRUST_RULE_DISABLED` | A rule changes. | The rule's id, issuer, principal, environment, repository, kinds and conditions. |
| `OIDC_TOKEN_EXCHANGED` | A CI token is issued, recorded as its service account. | The rule's id, the principal, environment, repository, `sha`, kinds, the SHA-256 of the ID token's issuer and `jti` (`jti_sha256`), the token's key id and expiry. Never a token. |
| `OIDC_EXCHANGE_REFUSED` | An exchange is refused. At most `LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE` rows per client address and minute; the next row says in `suppressed` how many were left out. | `code`, the issuer when it is a configured one, and, once the ID token verified, the environment and repository asked for, `jti_sha256` and the rule ids involved. Never a token. |

The `code` of a refusal is stable: `replayed`, `no_matching_rule`, `ambiguous_rules`,
`principal_inactive` or `commit_missing` after the ID token verified, and otherwise the
verifier's reason (`disabled`, `token_too_large`, `malformed`, `algorithm_not_allowed`,
`header_not_allowed`, `missing_key_id`, `issuer_not_allowed`, `unknown_key`, `keys_unavailable`,
`key_mismatch`, `weak_key`, `bad_signature`, `audience_mismatch`, `claim_missing`, `expired`,
`not_yet_valid`, `too_old`, `lifetime_too_long`).

A deploy run records the CI token that queued it, and a sign-in with a CI token is recorded as
`AUTHENTICATION_SUCCEEDED` once per token every `LAKANA_AUDIT__SIGN_IN_WINDOW_MINUTES`, like any
other token.
