import { describe, expect, it } from "vitest";

import {
  clearRunAs,
  createCiTrustRule,
  deleteCiTrustRule,
  explainCiTrust,
  getCiTrustRule,
  listCiTrustRules,
  setCiTrustRuleEnabled,
  setRunAs,
  updateCiTrustRule,
} from "./ciTrust";
import type { CiTrustRuleCreate } from "./ciTrust";
import { setStepUp } from "./client";

import { fakeServer } from "../test/server";

/** A session whose passkey touch is fresh for ten more minutes. */
const FRESH = {
  status: 200,
  body: { step_up_until: new Date(Date.now() + 10 * 60_000).toISOString() },
};

/** A rule as the server answers it. */
const RULE = {
  id: "7b0c7a52-6d0e-4f43-9a36-2f1f0d1c0a11",
  issuer: "https://token.actions.githubusercontent.com",
  principal: "ci-prod",
  environment: "prod",
  repository: "analytics",
  kinds: ["deploy"],
  conditions: { repository_id: "123456", ref: "refs/heads/main", event_name: "push" },
  enabled: true,
  created_by: "admin",
  created_at: "2026-11-20T09:00:00Z",
  updated_by: null,
  updated_at: "2026-11-20T09:00:00Z",
  start_on_deploy_jobs: false,
};

describe("ciTrust", () => {
  it("lists and reads rules without a passkey touch", async () => {
    const server = fakeServer({
      "GET /api/v1/ci-trust-rules": { status: 200, body: [RULE] },
      [`GET /api/v1/ci-trust-rules/${RULE.id}`]: { status: 200, body: RULE },
    });

    expect(await listCiTrustRules()).toEqual([RULE]);
    expect(await getCiTrustRule(RULE.id)).toEqual(RULE);
    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/ci-trust-rules",
      `GET /api/v1/ci-trust-rules/${RULE.id}`,
    ]);
  });

  it("asks for the passkey touch before every change of a rule", async () => {
    const path = `/api/v1/ci-trust-rules/${RULE.id}`;
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      "POST /api/v1/ci-trust-rules": { status: 201, body: RULE },
      [`PATCH ${path}`]: { status: 200, body: RULE },
      [`POST ${path}/enable`]: { status: 200, body: RULE },
      [`POST ${path}/disable`]: { status: 200, body: { ...RULE, enabled: false } },
      [`DELETE ${path}`]: { status: 204 },
    });
    setStepUp(() => Promise.resolve());
    const create: CiTrustRuleCreate = {
      issuer: "github",
      principal: "ci-prod",
      environment: "prod",
      repository: "analytics",
      kinds: ["deploy"],
      conditions: RULE.conditions,
      start_on_deploy_jobs: false,
    };

    await createCiTrustRule(create);
    await updateCiTrustRule(RULE.id, { principal: "ci-prod-2" });
    await setCiTrustRuleEnabled(RULE.id, true);
    expect((await setCiTrustRuleEnabled(RULE.id, false)).enabled).toBe(false);
    await deleteCiTrustRule(RULE.id);

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "POST /api/v1/ci-trust-rules",
      "GET /api/v1/sign-in/session",
      `PATCH ${path}`,
      "GET /api/v1/sign-in/session",
      `POST ${path}/enable`,
      "GET /api/v1/sign-in/session",
      `POST ${path}/disable`,
      "GET /api/v1/sign-in/session",
      `DELETE ${path}`,
    ]);
    expect(server.sent[1]?.body).toEqual(create);
    expect(server.sent[3]?.body).toEqual({ principal: "ci-prod-2" });
    // The interface never overrides a managed scope.
    expect(JSON.stringify(server.sent.map((request) => request.body))).not.toContain("override");
  });

  it("explains claims without a passkey touch, the issuer only when given", async () => {
    const report = {
      issuer: RULE.issuer,
      environment: "prod",
      repository: "analytics",
      verdict: `accepted by rule ${RULE.id}`,
      matching: [RULE.id],
      rules: [{ rule_id: RULE.id, matches: true, reasons: [] }],
    };
    const server = fakeServer({
      "POST /api/v1/ci-trust-rules/explain": { status: 200, body: report },
    });
    const claims = { iss: RULE.issuer, ref: "refs/heads/main" };

    expect(await explainCiTrust({ claims, environment: "prod", repository: "analytics" })).toEqual(
      report,
    );
    await explainCiTrust({
      claims,
      environment: "prod",
      repository: "analytics",
      issuer: "github",
    });

    expect(server.sent.map((request) => request.body)).toEqual([
      { claims, environment: "prod", repository: "analytics" },
      { claims, environment: "prod", repository: "analytics", issuer: "github" },
    ]);
  });

  it("binds a run-as account after the touch, and clears it without one", async () => {
    const path = "/api/v1/environments/pr od/deployments/ana lytics/run-as";
    const deployment = {
      repository: "ana lytics",
      project_version_id: "v-1",
      commit_sha: "0123456789abcdef0123456789abcdef01234567",
      updated_at: "2026-11-20T09:00:00Z",
      run_as: "etl-prod",
    };
    const server = fakeServer({
      "GET /api/v1/sign-in/session": FRESH,
      [`PUT ${path}`]: { status: 200, body: deployment },
      [`DELETE ${path}`]: { status: 200, body: { ...deployment, run_as: null } },
    });
    setStepUp(() => Promise.resolve());

    expect(await setRunAs("pr od", "ana lytics", "etl-prod")).toEqual(deployment);
    expect((await clearRunAs("pr od", "ana lytics")).run_as).toBeNull();

    expect(server.sent.map((request) => `${request.method} ${request.path}`)).toEqual([
      "GET /api/v1/sign-in/session",
      "PUT /api/v1/environments/pr%20od/deployments/ana%20lytics/run-as",
      "DELETE /api/v1/environments/pr%20od/deployments/ana%20lytics/run-as",
    ]);
    expect(server.sent[1]?.body).toEqual({ principal: "etl-prod" });
  });
});
