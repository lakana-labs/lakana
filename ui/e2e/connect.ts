/**
 * Stubs for the end-to-end tests of Connect a repository: the host's keys, the repository and
 * its deploy keys, and an access test the git host refuses.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

const AT = "2026-11-11T08:00:00Z";
/** The probe of the host's keys, done. */
const HOST_KEYS = {
  id: "p-1",
  kind: "host_keys",
  state: "done",
  outcome: "ok",
  message: "Found 2 host keys of github.com.",
  host_keys: [
    {
      type: "ssh-ed25519",
      fingerprint: "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
      line: "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    },
    {
      type: "ssh-rsa",
      fingerprint: "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
      line: "github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt",
    },
  ],
  created_at: AT,
  finished_at: AT,
};
/** The access test of `dev`, which the git host refused. */
const ACCESS = {
  ...HOST_KEYS,
  id: "p-2",
  kind: "access",
  outcome: "auth_refused",
  message:
    "The git host refused the key of dev: add it as a read-only deploy key of the repository, then test again.",
  host_keys: null,
};
/** The deploy keys made for the new repository: `dev` new, `prod` kept, `staging` skipped. */
export const DEPLOY_KEY = {
  secret: "analytics-deploy-key",
  keys: ["dev", "prod"].map((environment, index) => ({
    environment,
    public_key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL${environment}Q0r8VHx2mPz7Ykq lakana analytics ${environment}`,
    fingerprint: `SHA256:${environment}7Kq2xY0m8vPz3Lr9Wc4Tn6Hb1Jd5Fg0Ns2Qe`,
    made: index === 0,
  })),
  skipped: ["staging"],
  replaced: [],
  notes: ["The key of prod was made earlier; it stays as it is."],
};
/** The paths {@link stubConnect} answers, beside those of `session.ts`. */
export const ROUTES = [
  "/api/v1/repository-probes",
  "/api/v1/repositories",
  "/api/v1/repositories/analytics/deploy-key",
];

/**
 * Answers the routes of the page like the server: the host's keys at once, the repository and
 * its keys, and an access test the git host refuses.
 *
 * @param page - The page.
 */
export async function stubConnect(page: Page): Promise<void> {
  await page.route(
    (url) => url.pathname === "/api/v1/repository-probes",
    async (route) => {
      const body = route.request().postDataJSON() as { kind: string };
      await route.fulfill({ status: 202, json: body.kind === "access" ? ACCESS : HOST_KEYS });
    },
  );
  await page.route(
    (url) => url.pathname === "/api/v1/repositories",
    (route) => route.fulfill({ status: 201, json: { name: "analytics" } }),
  );
  await page.route("**/api/v1/repositories/analytics/deploy-key", (route) =>
    route.fulfill({ status: 201, json: DEPLOY_KEY }),
  );
}
