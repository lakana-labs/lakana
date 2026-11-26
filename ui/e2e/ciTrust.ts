/**
 * Stubs for the end-to-end tests of Settings › CI trust: two rules for `analytics` (prod,
 * enabled; dev, dry runs only and disabled), which change as the tests add, edit, enable,
 * disable and delete; Explain; the repositories `analytics` and `dbt-models`; the environments
 * dev and prod; and the access files' status, managing nothing or prod.
 *
 * @packageDocumentation
 */
import type { Page, Route } from "@playwright/test";

const AT = "2026-11-20T09:00:00Z";

/** The prod rule's id. */
export const PROD_RULE = "7b0c7a52-6d0e-4f43-9a36-2f1f0d1c0a11";

/** The dev rule's id. */
export const DEV_RULE = "1f2e3d4c-5b6a-4789-8a9b-0c1d2e3f4a5b";

/** The id the stubs give a new rule. */
export const NEW_RULE = "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a";

/** A rule as the stubs keep it. */
interface Rule {
  readonly id: string;
  readonly issuer: string;
  readonly principal: string;
  readonly environment: string;
  readonly repository: string;
  readonly kinds: readonly string[];
  readonly conditions: Readonly<Record<string, string>>;
  readonly enabled: boolean;
  readonly created_by: string | null;
  readonly created_at: string;
  readonly updated_by: string | null;
  readonly updated_at: string;
  readonly start_on_deploy_jobs: boolean;
}

const GITHUB = "https://token.actions.githubusercontent.com";

/**
 * The two rules the stubs start with.
 *
 * @returns The rules, oldest first.
 */
function firstRules(): Rule[] {
  return [
    {
      id: DEV_RULE,
      issuer: GITHUB,
      principal: "ci-dev",
      environment: "dev",
      repository: "analytics",
      kinds: ["dry_run"],
      conditions: { repository_id: "123456", environment: "lakana-dry-run" },
      enabled: false,
      created_by: "admin",
      created_at: "2026-11-19T09:00:00Z",
      updated_by: "maria",
      updated_at: "2026-11-21T10:00:00Z",
      start_on_deploy_jobs: false,
    },
    {
      id: PROD_RULE,
      issuer: GITHUB,
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
      created_at: AT,
      updated_by: null,
      updated_at: AT,
      start_on_deploy_jobs: true,
    },
  ];
}

/** The paths {@link stubCiTrust} answers, beside those of `session.ts`. */
export const CI_TRUST_STUBBED = [
  "/api/v1/ci-trust-rules",
  "/api/v1/ci-trust-rules/explain",
  ...[PROD_RULE, DEV_RULE, NEW_RULE].flatMap((id) => [
    `/api/v1/ci-trust-rules/${id}`,
    `/api/v1/ci-trust-rules/${id}/enable`,
    `/api/v1/ci-trust-rules/${id}/disable`,
  ]),
  "/api/v1/access/status",
  "/api/v1/repositories",
  "/api/v1/environments",
  "/api/v1/sign-in/step-up/options",
  "/api/v1/sign-in/step-up",
];

/**
 * Fulfils a route with JSON.
 *
 * @param route - The route.
 * @param json - The body.
 * @param status - The status.
 * @returns When fulfilled.
 */
function answer(route: Route, json: unknown, status = 200): Promise<void> {
  return route.fulfill({ status, json });
}

/** A registered repository. */
const REPOSITORY = {
  kind: "ssh",
  credential_secret: null,
  credential_username: null,
  ssh_known_hosts: null,
  default_ref: null,
  created_at: AT,
  updated_at: AT,
  deploy_keys: [],
};

/**
 * Answers CI trust: the list, each rule's page, add, edit, enable, disable, delete and Explain
 * (which accepts the claims of a push to main for the prod rule, and refuses any other), the
 * repositories and environments a rule may name, and the access files' status. Register it
 * after `signInAsAdmin`.
 *
 * @param page - The page.
 * @param managed - The scopes an access file manages; none by default.
 */
export async function stubCiTrust(page: Page, managed: readonly string[] = []): Promise<void> {
  let rules = firstRules();
  await page.route("**/api/v1/access/status", (route) =>
    answer(route, {
      scopes: managed.map((scope) => ({
        scope,
        file_name: "access.yaml",
        file_sha256: "ab12cd",
        source_commit: null,
        applied_by: "admin",
        applied_at: AT,
      })),
      overrides: [],
    }),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/repositories",
    (route) =>
      answer(route, [
        { ...REPOSITORY, id: "r-1", name: "analytics", url: "git@github.com:corp/analytics.git" },
        {
          ...REPOSITORY,
          id: "r-2",
          name: "dbt-models",
          url: "git@github.com:corp/dbt-models.git",
        },
      ]),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/environments",
    (route) =>
      answer(
        route,
        ["dev", "prod"].map((name) => ({
          name,
          storage_root: `s3://corp-lake/${name}`,
          storage_secret_ref: null,
          is_protected: name === "prod",
          properties: {},
          retention_days: null,
          deleted_at: null,
          purge_after: null,
        })),
      ),
  );
  await page.route(
    (url) => url.pathname === "/api/v1/ci-trust-rules",
    (route) => {
      const request = route.request();
      if (request.method() !== "POST") return answer(route, rules);
      const body = request.postDataJSON() as Omit<Rule, "id" | "enabled" | "issuer"> & {
        issuer: string;
      };
      const made: Rule = {
        ...body,
        id: NEW_RULE,
        issuer: body.issuer === "github" ? GITHUB : body.issuer,
        enabled: true,
        created_by: "admin",
        created_at: AT,
        updated_by: null,
        updated_at: AT,
      };
      rules = [...rules, made];
      return answer(route, made, 201);
    },
  );
  await page.route(
    (url) => url.pathname === "/api/v1/ci-trust-rules/explain",
    (route) => {
      const { claims, environment, repository } = route.request().postDataJSON() as {
        claims: Record<string, unknown>;
        environment: string;
        repository: string;
      };
      const accepted =
        environment === "prod" && repository === "analytics" && claims["ref"] === "refs/heads/main";
      return answer(route, {
        issuer: GITHUB,
        environment,
        repository,
        verdict: accepted
          ? `accepted by rule ${PROD_RULE}`
          : "refused: no enabled rule for this issuer, environment and repository matches",
        matching: accepted ? [PROD_RULE] : [],
        rules: rules.map((rule) => {
          const reasons =
            rule.id === PROD_RULE && accepted
              ? []
              : [`the rule is for environment '${rule.environment}', not '${environment}'`];
          return { rule_id: rule.id, matches: reasons.length === 0, reasons };
        }),
      });
    },
  );
  await page.route(
    (url) => /^\/api\/v1\/ci-trust-rules\/[0-9a-f-]+(\/(enable|disable))?$/.test(url.pathname),
    (route) => {
      const request = route.request();
      const [, id = "", action = ""] =
        /^\/api\/v1\/ci-trust-rules\/([0-9a-f-]+)(?:\/(enable|disable))?$/.exec(
          new URL(request.url()).pathname,
        ) ?? [];
      const found = rules.find((rule) => rule.id === id);
      if (found === undefined) {
        return answer(route, { type: "NotFoundError", detail: `No CI trust rule ${id}.` }, 404);
      }
      const replace = (next: Rule): Promise<void> => {
        rules = rules.map((rule) => (rule.id === id ? next : rule));
        return answer(route, next);
      };
      if (request.method() === "DELETE") {
        rules = rules.filter((rule) => rule.id !== id);
        return route.fulfill({ status: 204 });
      }
      if (action !== "") {
        return replace({ ...found, enabled: action === "enable", updated_by: "admin" });
      }
      if (request.method() === "PATCH") {
        return replace({
          ...found,
          ...(request.postDataJSON() as Partial<Rule>),
          updated_by: "admin",
        });
      }
      return answer(route, found);
    },
  );
}
