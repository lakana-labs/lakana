/**
 * The session routes, stubbed with `page.route`, so the interface runs against `vite preview`
 * without a server.
 *
 * @packageDocumentation
 */
import type { Page } from "@playwright/test";

/** The session of a signed-in administrator, as `GET /api/v1/sign-in/session` answers it. */
export const ADMIN_SESSION = {
  name: "admin",
  display_name: null,
  type: "USER",
  via: "passkey",
  csrf_token: "csrf-e2e",
  step_up_until: null,
  needs_passkey: false,
  offer_passkey: false,
  absolute_expires_at: "2099-01-01T00:00:00Z",
};

/** The paths the stubs answer; any other request to the API is a test failure. */
export const STUBBED = ["/api/v1/sign-in/session", "/api/v1/sign-in/notices", "/api/v1/whoami"];

/**
 * Answers the session routes as for a signed-in administrator without notices.
 *
 * @param page - The page.
 */
export async function signInAsAdmin(page: Page): Promise<void> {
  await page.route("**/api/v1/sign-in/session", (route) => route.fulfill({ json: ADMIN_SESSION }));
  await page.route("**/api/v1/sign-in/notices", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/v1/whoami", (route) =>
    route.fulfill({
      json: {
        name: "admin",
        type: "USER",
        task_token: null,
        ci_token: null,
        language: null,
        admin: true,
        has_access: true,
      },
    }),
  );
}
