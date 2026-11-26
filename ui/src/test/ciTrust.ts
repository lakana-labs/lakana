/**
 * What the server answers Settings › CI trust with, for tests: rules, the repositories and the
 * environments a rule may name.
 *
 * @packageDocumentation
 */
import type { CiTrustRuleOut } from "../api/ciTrust";
import type { RepositoryOut } from "../api/repositories";
import { accessStatusRoute, anEnvironment } from "./access";
import type { FakeRoute } from "./server";

/** The prod rule's id. */
export const PROD_RULE = "7b0c7a52-6d0e-4f43-9a36-2f1f0d1c0a11";

/** The dev rule's id. */
export const DEV_RULE = "1f2e3d4c-5b6a-4789-8a9b-0c1d2e3f4a5b";

/**
 * A rule: GitHub Actions may deploy `analytics` to `prod` as `ci-prod`, from pushes to main.
 *
 * @param over - What differs.
 * @returns The rule.
 */
export function aRule(over: Partial<CiTrustRuleOut> = {}): CiTrustRuleOut {
  return {
    id: PROD_RULE,
    issuer: "https://token.actions.githubusercontent.com",
    principal: "ci-prod",
    environment: "prod",
    repository: "analytics",
    kinds: ["deploy"],
    conditions: {
      repository_id: "123456",
      ref: "refs/heads/main",
      event_name: "push",
      environment: "prod",
    },
    enabled: true,
    created_by: "admin",
    created_at: "2026-11-20T09:00:00Z",
    updated_by: null,
    updated_at: "2026-11-20T09:00:00Z",
    start_on_deploy_jobs: false,
    ...over,
  };
}

/**
 * The dev rule: dry runs only, from the GitHub environment `lakana-dry-run`, disabled.
 *
 * @param over - What differs.
 * @returns The rule.
 */
export function aDevRule(over: Partial<CiTrustRuleOut> = {}): CiTrustRuleOut {
  return aRule({
    id: DEV_RULE,
    principal: "ci-dev",
    environment: "dev",
    kinds: ["dry_run"],
    conditions: { repository_id: "123456", environment: "lakana-dry-run" },
    enabled: false,
    created_at: "2026-11-19T09:00:00Z",
    updated_at: "2026-11-21T10:00:00Z",
    updated_by: "maria",
    ...over,
  });
}

/**
 * A registered repository.
 *
 * @param name - Its name.
 * @param url - Its URL.
 * @returns The repository.
 */
function aRepository(name: string, url = `git@github.com:corp/${name}.git`): RepositoryOut {
  return {
    id: `${name}-id`,
    name,
    kind: "ssh",
    url,
    credential_secret: null,
    credential_username: null,
    ssh_known_hosts: null,
    default_ref: null,
    created_at: "2026-11-01T09:00:00Z",
    updated_at: "2026-11-01T09:00:00Z",
    deploy_keys: [],
  };
}

/**
 * The routes of CI trust: the two rules, the repositories `analytics` and `dbt`, the
 * environments `dev` and `prod`, and no managed scope.
 *
 * @param more - Routes that differ.
 * @returns The routes.
 */
export function ciTrustRoutes(more: Record<string, FakeRoute> = {}): Record<string, FakeRoute> {
  return {
    ...accessStatusRoute(),
    "GET /api/v1/ci-trust-rules": { status: 200, body: [aDevRule(), aRule()] },
    [`GET /api/v1/ci-trust-rules/${PROD_RULE}`]: { status: 200, body: aRule() },
    [`GET /api/v1/ci-trust-rules/${DEV_RULE}`]: { status: 200, body: aDevRule() },
    "GET /api/v1/repositories": {
      status: 200,
      body: [aRepository("analytics"), aRepository("dbt", "https://gitlab.com/corp/dbt.git")],
    },
    "GET /api/v1/environments": {
      status: 200,
      body: [anEnvironment("dev"), { ...anEnvironment("prod"), is_protected: true }],
    },
    "GET /api/v1/principals": { status: 200, body: [] },
    ...more,
  };
}
