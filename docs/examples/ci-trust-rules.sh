#!/usr/bin/env bash
set -euo pipefail
# The commands of the how-tos "Deploy from GitHub Actions without a stored secret" and "Deploy
# from GitLab CI without a stored secret". The pages show the regions.
#
# Not an example the runner starts (it needs a server with CI OIDC on and a CI provider):
# packages/lakana-server/tests/cli/test_ci_oidc_docs.py runs every command of every region with
# the CLI against the real app, checks with `ci-trust explain` that the rules accept the jobs of
# the CI files `lakana init` writes (and refuse a pull request's deploy), and deploys through the
# CLI with an ID token. packages/lakana/tests/cli/test_project_init.py checks that those CI files
# name the same `ci-trust create` commands in their comments.

# docs:start accounts
lakana principal create ci-dev --type service-account
lakana principal create ci-prod --type service-account
lakana grant create --principal ci-dev --action DEPLOY --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --action DEPLOY --kind ENVIRONMENT --env prod
lakana grant create --principal ci-dev --role VIEWER --kind ENVIRONMENT --env dev
lakana grant create --principal ci-prod --role VIEWER --kind ENVIRONMENT --env prod
# docs:end accounts

# docs:start github
lakana ci-trust create --issuer github --principal ci-dev --env dev --repo analytics \
  --kind dry_run --claim repository_id=123456 --claim environment=lakana-dry-run
lakana ci-trust create --issuer github --principal ci-prod --env prod --repo analytics \
  --claim repository_id=123456 --claim ref=refs/heads/main \
  --claim event_name=push --claim environment=prod
# docs:end github

# docs:start gitlab
lakana ci-trust create --issuer gitlab --principal ci-dev --env dev --repo analytics \
  --kind dry_run --claim project_id=123456 --claim environment=lakana-dry-run
lakana ci-trust create --issuer gitlab --principal ci-prod --env prod --repo analytics \
  --claim project_id=123456 --claim ref=main --claim ref_type=branch \
  --claim ref_protected=true --claim environment=prod
# docs:end gitlab

# docs:start both-kinds
lakana ci-trust update RULE_ID --kind deploy --kind dry_run
# docs:end both-kinds

# docs:start start-jobs
lakana ci-trust update RULE_ID --start-on-deploy-jobs
# docs:end start-jobs

# docs:start explain
lakana ci-trust explain --claims claims.json --env prod --repo analytics
# docs:end explain

# docs:start disable
lakana ci-trust disable RULE_ID
# docs:end disable
